"""Telegram bot feature wiring for the dashboard.

The generic Telegram plumbing lives in app/telegram.py; this module is the
feature registry. Features:

- daily watering reminders (09:00 & 21:00, server-local time) with an
  on-demand `/plantes` command;
- `/envies` — read the household wishlists (spoiler-free: the chat is shared,
  so reservations show as taken without naming who took them);
- **adding a wish in plain French** — `/envie un casque Sony vers 350.- pour
  Lea`, or simply writing it in a private chat. The message goes through
  `nlu.parse_wish_message()`, and whatever is missing (who it is for, the
  price, a link) is asked as a follow-up question. `/moi <prénom>` links a
  Telegram account to a person so « je veux… » needs no follow-up.
- **show tracker digest** — `/series` reads what's being watched and what's
  next; a daily sync (`sync_and_notify_shows`) re-pulls followed shows from
  TMDb and announces episodes that just aired and are not yet marked watched.

Future features should register their own cron jobs here (and in
main.startup) via `add_cron_job()`.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta
from itertools import groupby
from zoneinfo import ZoneInfo

from fastapi import APIRouter

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import telegram, tmdb
from .config import OPENROUTER_API_KEY, TELEGRAM_TZ
from .database import get_conn, rows_to_dicts
from .nlu import parse_storage_message, parse_wish_details, parse_wish_message
from .openrouter import LLMError
from .routers.plants import due_plants
from .routers.shows import list_shows, refresh_show
from .routers.storage import (
    InIn,
    ItemIn,
    OutIn,
    create_item as storage_create_item,
    list_boxes as storage_list_boxes,
    list_items as storage_list_items,
    normalize as storage_normalize,
    put_back as storage_put_back,
    storage_summary,
    take_out as storage_take_out,
)
from .routers.wishlist import (
    TelegramLinkIn,
    WishIn,
    create_item,
    get_item,
    link_telegram_user,
    list_items,
    list_people,
    match_people,
    person_by_telegram_user,
    update_item,
)
from .tmdb import TMDbError

router = APIRouter(prefix="/api/bot", tags=["bot"])

WATERING_HOURS = (9, 21)  # daily checks, server-local time
SHOWS_SYNC_HOUR = 8            # the daily show sync + digest is due from 08:00…
SHOWS_SYNC_CHECK_MINUTES = 15  # …and checked this often until it has run (see shows_sync_due)

_scheduler: BackgroundScheduler | None = None
_cron_jobs: list[tuple] = []  # (func, hour, minute)
_last_shows_sync: datetime | None = None  # server-local, set by sync_and_notify_shows


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


def _plants_command(args: str, chat_id: int, user_id: int) -> str:
    return watering_message()


PRIORITY_STARS = {1: "⭐", 2: "⭐⭐", 3: "⭐⭐⭐"}


def wishlist_message(person_filter: str = "") -> str:
    """Build the French wishlist summary. Never names who reserved what."""
    people = list_people()
    if person_filter:
        # Même résolution que partout ailleurs : « seb » trouve « Séb ».
        people = match_people(person_filter)
        if not people:
            return f"🎁 Aucune liste d'envies pour « {person_filter.strip()} »"
    if not people:
        return "🎁 Envies\n\nAucune liste pour l'instant — créez-en une dans l'onglet Envies."

    lines = ["🎁 Envies", ""]
    for person in people:
        header = f"{person['emoji']} {person['name']}"
        days = person["days_until_birthday"]
        if days is not None and days <= 60:
            header += f" — anniversaire dans {days} j 🎂"
        lines.append(header)

        items = [i for i in list_items(person_id=person["id"]) if i["status"] != "archived"]
        if not items:
            lines.append("   (liste vide)")
        for item in items[:8]:
            price = f" · {item['price']:g} {item['currency']}" if item["price"] else ""
            if item["status"] == "received":
                mark = " ✅ offert"
            elif item["is_taken"]:
                mark = " 🔒 pris en charge"
            else:
                mark = ""
            lines.append(f"   • {PRIORITY_STARS.get(item['priority'], '')} {item['name']}{price}{mark}")
        if len(items) > 8:
            lines.append(f"   … et {len(items) - 8} autre(s)")

        open_items = [i for i in items if i["status"] == "wanted"]
        if open_items:
            budget = sum(i["price"] or 0 for i in open_items)
            recap = f"   {len(open_items)} envie(s) à offrir"
            if budget:
                recap += f" · ~{budget:g} {open_items[0]['currency']}"
            lines.append(recap)
        lines.append("")
    return "\n".join(lines).rstrip()


def _wishlist_command(args: str, chat_id: int, user_id: int) -> str:
    return wishlist_message(args)


# ---------------- Séries & films ----------------

def _fmt_air_date(iso: str) -> str:
    try:
        return datetime.strptime(iso[:10], "%Y-%m-%d").strftime("%d/%m")
    except ValueError:
        return iso


def shows_message() -> str:
    """Build the French « what am I watching » digest for /series."""
    watching = list_shows(status="en_cours", media_type="tv")
    lines = ["🎬 Séries en cours", ""]
    if not watching:
        lines.append("Rien en cours — ajoutez une série dans l'onglet Séries.")
    for s in watching:
        new = s["unwatched_aired_episodes"]
        mark = f" 🆕 {new} épisode(s) à voir" if new else " · à jour ✅"
        next_bit = ""
        if s["next_episode"] and s["next_episode"]["air_date"]:
            ne = s["next_episode"]
            next_bit = f"\n   Prochain : S{ne['season_number']:02d}E{ne['episode_number']:02d} le {_fmt_air_date(ne['air_date'])}"
        lines.append(f"• {s['title']}{mark}{next_bit}")

    to_watch_movies = list_shows(status="a_voir", media_type="movie")
    if to_watch_movies:
        lines += ["", f"🍿 {len(to_watch_movies)} film(s) à voir"]
    return "\n".join(lines)


def _shows_command(args: str, chat_id: int, user_id: int) -> str:
    return shows_message()


def _ep_code(e: dict) -> str:
    return f"S{e['season_number']:02d}E{e['episode_number']:02d}"


def shows_sync_due() -> bool:
    """Whether the latest 08:00 slot (yesterday's, before 08:00) has gone by
    without a show sync.

    A plain 08:00 cron missed it most mornings: the host sleeps overnight and
    wakes after 08:00, and APScheduler drops a run that is more than a second
    late — or never hears of it at all when the app wasn't running (reboot,
    container restart). So the sync is checked every few minutes instead
    (`_shows_sync_check`) and runs as soon as it is due.

    Right after a restart the in-memory `_last_shows_sync` is empty; the
    oldest `last_synced_at` among the shows the sync covers stands in for it
    (not the newest — refreshing a single show by hand must not count)."""
    now = datetime.now(ZoneInfo(TELEGRAM_TZ)) if TELEGRAM_TZ else datetime.now().astimezone()
    slot = now.replace(hour=SHOWS_SYNC_HOUR, minute=0, second=0, microsecond=0)
    if slot > now:
        slot -= timedelta(days=1)
    slot = slot.astimezone().replace(tzinfo=None)  # server-local, like every stored timestamp

    last = _last_shows_sync
    if last is None:
        with get_conn() as conn:
            oldest = conn.execute(
                "SELECT MIN(last_synced_at) FROM shows WHERE media_type = 'tv' AND status != 'abandonne'"
            ).fetchone()[0]
        if oldest is None:
            return False  # rien à synchroniser
        last = datetime.fromisoformat(oldest)
    return last < slot


def _shows_sync_check() -> None:
    """Every SHOWS_SYNC_CHECK_MINUTES: run the daily show sync once it's due."""
    if tmdb.is_configured() and shows_sync_due():
        sync_and_notify_shows()


def sync_and_notify_shows() -> bool:
    """Daily: re-sync tracked shows from TMDb, then announce episodes that just
    aired and are not yet marked watched (each episode is announced only once,
    grouped into one line per show).

    Every non-abandoned show is re-synced, including `termine` ones: TMDb can
    renew a show after we'd caught up on everything that existed so far, and
    `refresh_show` reopens it to `en_cours` when that happens — skipping
    `termine` here would mean that reopening (and the notification for the
    episode that triggered it) never happens."""
    if not tmdb.is_configured():
        return False

    with get_conn() as conn:
        show_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM shows WHERE media_type = 'tv' AND status != 'abandonne'"
        ).fetchall()]
    for show_id in show_ids:
        try:
            refresh_show(show_id)
        except TMDbError:
            continue
    global _last_shows_sync
    _last_shows_sync = datetime.now()

    with get_conn() as conn:
        due = rows_to_dicts(conn.execute(
            """SELECT e.id, e.season_number, e.episode_number, e.name, s.id AS show_id, s.title
               FROM show_episodes e JOIN shows s ON s.id = e.show_id
               WHERE e.air_date IS NOT NULL AND e.air_date <= date('now')
                 AND e.watched_at IS NULL AND e.notified_at IS NULL
                 AND s.status IN ('a_voir', 'en_cours')
               ORDER BY s.title, s.id, e.season_number, e.episode_number"""
        ).fetchall())
    if not due:
        return False

    # Une ligne par série : un rattrapage de 80 épisodes reste une ligne lisible.
    lines = ["📺 Nouveaux épisodes disponibles", ""]
    for _, group in groupby(due, key=lambda e: e["show_id"]):
        eps = list(group)
        first, last = eps[0], eps[-1]
        if len(eps) == 1:
            title_bit = f" « {first['name']} »" if first["name"] else ""
            lines.append(f"• {first['title']} — {_ep_code(first)}{title_bit}")
        else:
            lines.append(f"• {first['title']} — {len(eps)} épisodes ({_ep_code(first)} → {_ep_code(last)})")
    sent = telegram.send_message("\n".join(lines))
    if sent:
        with get_conn() as conn:
            conn.executemany(
                "UPDATE show_episodes SET notified_at = ? WHERE id = ?",
                [(datetime.now().isoformat(timespec="seconds"), e["id"]) for e in due],
            )
    return sent


# ---------------- Ajouter une envie en langage naturel ----------------

# Une conversation en cours par chat : le bot pose une question, la réponse
# suivante de ce chat y répond. Volontairement en mémoire — un redémarrage
# oublie les questions en suspens, ce qui vaut mieux que de répondre à côté.
_pending: dict[int, dict] = {}
PENDING_TTL = timedelta(minutes=15)

CANCEL_WORDS = {"annuler", "stop", "laisse tomber", "oublie", "non merci"}
SKIP_WORDS = {"non", "nan", "rien", "passe", "skip", "je sais pas", "aucune idée", "sais pas"}
# Politesses qu'on retire avant de chercher un prénom : « pour Léa » → « Léa ».
_LEAD_IN = re.compile(r"^(?:c'est\s+)?(?:pour|à|a)\s+", re.IGNORECASE)


def _remember(chat_id: int, **state) -> None:
    _pending[chat_id] = {**state, "expires_at": datetime.now() + PENDING_TTL}


def _pending_for(chat_id: int) -> dict | None:
    state = _pending.get(chat_id)
    if state and datetime.now() > state["expires_at"]:
        _pending.pop(chat_id, None)
        return None
    return state


def _forget(chat_id: int) -> None:
    _pending.pop(chat_id, None)


def _people_names() -> str:
    return ", ".join(p["name"] for p in list_people()) or "aucune liste pour l'instant"


def _join_fr(parts: list[str]) -> str:
    """« a », « a et b », « a, b et c » — pas « a et b et c »."""
    if len(parts) < 2:
        return "".join(parts)
    return f"{', '.join(parts[:-1])} et {parts[-1]}"


def _resolve_target(hint: str, for_sender: bool, user_id: int) -> tuple[dict | None, str | None]:
    """(person, question). A question means: ask the chat and try again."""
    hint = _LEAD_IN.sub("", (hint or "").strip())
    if hint:
        matches = match_people(hint)
        if len(matches) == 1:
            return matches[0], None
        if matches:
            names = ", ".join(m["name"] for m in matches)
            return None, f"Plusieurs personnes correspondent à « {hint} » : {names}. Laquelle ?"
        return None, f"Je ne connais pas « {hint} ». C'est pour qui ? ({_people_names()})"

    if for_sender:
        me = person_by_telegram_user(user_id)
        if me:
            return me, None
        return None, (
            f"C'est pour qui ? ({_people_names()})\n"
            "Astuce : envoie « /moi <prénom> » une fois et je te reconnaîtrai ensuite."
        )
    return None, f"C'est pour qui ? ({_people_names()})"


def _missing_labels(wish: dict) -> list[str]:
    """What is worth asking about for this wish — kept short to avoid nagging."""
    missing = []
    if not wish["price"]:
        missing.append("le prix")
    if not wish["url"] and not wish["shop"]:
        missing.append("un lien ou le magasin")
    if wish["category"] == "vetements" and not wish["size"]:
        missing.append("la taille")
    return missing


def _ask_next_detail(chat_id: int, queue: list[int]) -> str | None:
    """Ask about the first wish in the queue that is still incomplete."""
    while queue:
        wish = get_item(queue[0])
        missing = _missing_labels(wish)
        if not missing:
            queue.pop(0)
            continue
        _remember(chat_id, stage="details", queue=queue)
        return (
            f"Pour « {wish['name']} », il me manque {_join_fr(missing)}. "
            "Réponds-moi, ou « non » pour laisser comme ça."
        )
    _forget(chat_id)
    return None


def _wish_line(wish: dict) -> str:
    bits = [f"• {wish['name']}"]
    if wish["price"]:
        bits.append(f"{wish['price']:g} {wish['currency']}")
    if wish["shop"]:
        bits.append(wish["shop"])
    if wish["size"]:
        bits.append(f"taille {wish['size']}")
    return " · ".join(bits)


def _create_wishes(chat_id: int, person: dict, items: list[dict]) -> str:
    created = [create_item(WishIn(person_id=person["id"], **item)) for item in items]
    lines = [f"🎁 Ajouté à la liste de {person['name']} :"] + [_wish_line(w) for w in created]
    question = _ask_next_detail(chat_id, [w["id"] for w in created])
    if question:
        lines += ["", question]
    return "\n".join(lines)


def _patch_wish(wish: dict, fields: dict) -> dict:
    """Apply a partial update through the normal PUT payload (status untouched)."""
    payload = {
        key: wish[key] for key in (
            "person_id", "name", "description", "category", "price", "currency", "url",
            "shop", "size", "color", "quantity", "priority", "occasion", "target_date", "notes",
        )
    }
    payload.update(fields)
    return update_item(wish["id"], WishIn(**payload))


def _apply_details(text: str, chat_id: int, state: dict) -> str:
    queue = state["queue"]
    wish = get_item(queue[0])

    if text.strip().casefold() in SKIP_WORDS:
        confirmation = f"Ok, « {wish['name']} » reste tel quel."
    else:
        try:
            parsed = parse_wish_details(text, wish, _missing_labels(wish))
        except LLMError as e:
            return f"Je n'ai pas réussi à lire ta réponse ({e}). Tu peux reformuler ou dire « non »."
        if parsed["skip"]:
            confirmation = f"Ok, « {wish['name']} » reste tel quel."
        else:
            updated = _patch_wish(wish, parsed["fields"])
            confirmation = "✅ " + _wish_line(updated)

    queue.pop(0)
    question = _ask_next_detail(chat_id, queue)
    return confirmation + (f"\n\n{question}" if question else "")


def _start_wish(text: str, chat_id: int, user_id: int, forced: bool = False) -> str | None:
    """Parse a message into wishes. `forced` = explain failures instead of staying silent."""
    people = list_people()
    if not people:
        return "Aucune liste d'envies n'existe encore — crée d'abord une personne dans l'onglet Envies 🎁" if forced else None

    try:
        parsed = parse_wish_message(text, [p["name"] for p in people])
    except LLMError as e:
        return f"Je n'ai pas réussi à analyser ça ({e})." if forced else None

    if parsed["intent"] != "wish":
        return (
            "Je n'ai pas repéré d'envie là-dedans. Essaie par exemple :\n"
            "« /envie un casque Sony vers 350.- chez Digitec pour Léa »"
        ) if forced else None

    person, question = _resolve_target(parsed["person"], parsed["for_sender"], user_id)
    if person is None:
        _remember(chat_id, stage="person", items=parsed["items"])
        recap = ", ".join(i["name"] for i in parsed["items"])
        return f"Noté : {recap}.\n{question}"
    return _create_wishes(chat_id, person, parsed["items"])


def _wish_text_handler(text: str, chat_id: int, user_id: int, is_private: bool) -> str | None:
    """Plain (non-command) messages. Returning None keeps the bot quiet."""
    state = _pending_for(chat_id)
    if state:
        if text.strip().casefold() in CANCEL_WORDS:
            _forget(chat_id)
            return "Ok, j'oublie ça."
        if state["stage"] == "person":
            person, question = _resolve_target(text, False, user_id)
            if person is None:
                _remember(chat_id, stage="person", items=state["items"])  # refresh the TTL
                return question
            return _create_wishes(chat_id, person, state["items"])
        if state["stage"] == "details":
            return _apply_details(text, chat_id, state)

    # Hors conversation : on n'analyse le bavardage qu'en tête-à-tête. Dans un
    # groupe il faut « /envie … », sinon chaque message partirait vers le LLM.
    return _start_wish(text, chat_id, user_id) if is_private else None


def _wish_add_command(args: str, chat_id: int, user_id: int) -> str:
    if not args:
        return (
            "Dis-moi ce qui te ferait plaisir, par exemple :\n"
            "« /envie un casque Sony vers 350.- chez Digitec »"
        )
    return _start_wish(args, chat_id, user_id, forced=True)


def _me_command(args: str, chat_id: int, user_id: int) -> str:
    """Link this Telegram account to a person, so « je veux… » needs no follow-up."""
    if not args:
        return f"Dis-moi qui tu es : « /moi <prénom> » ({_people_names()})"
    matches = match_people(args)
    if not matches:
        return f"Je ne connais pas « {args} » ({_people_names()})"
    if len(matches) > 1:
        names = ", ".join(m["name"] for m in matches)
        return f"Plusieurs personnes correspondent : {names}. Sois plus précis·e."
    person = link_telegram_user(matches[0]["id"], TelegramLinkIn(telegram_user_id=user_id))
    return f"Ok {person['name']} ! Quand tu diras « je veux… », je saurai que c'est pour toi 🎁"


def _help_command(args: str, chat_id: int, user_id: int) -> str:
    return (
        "Commandes :\n"
        "/plantes — état de l'arrosage\n"
        "/envies [prénom] — listes d'envies de la maison (sans spoiler)\n"
        "/envie <texte libre> — ajouter une envie « un casque Sony vers 350.- pour Léa »\n"
        "/moi <prénom> — me dire qui tu es, pour que « je veux… » aille sur ta liste\n"
        "\n"
        "Rangement :\n"
        "/cartons [recherche] — inventaire des cartons, ou recherche d'un objet\n"
        "/ou <objet> — dans quel carton il se trouve\n"
        "/sortis — ce qui est actuellement hors carton\n"
        "/sorti <texte> — « /sorti le wetsuit long du carton 2, prêté à Tom »\n"
        "/range <texte> — « /range le stéthoscope »\n"
        "\n"
        "Séries & films :\n"
        "/series — ce qui est en cours, les nouveaux épisodes, les films à voir\n"
        "\n"
        "En message privé, pas besoin de commande : écris ton envie ou ce que tu sors "
        "d'un carton normalement, je comprends et je mets à jour."
    )


# ---------------- Rangement : sortir / ranger en langage naturel ----------------

# Mots qui trahissent un message de rangement. Le handler texte ne réveille le
# LLM que si l'un d'eux apparaît — sinon chaque bavardage partirait au modèle,
# alors que c'est la liste d'envies qui doit récupérer le message.
STORAGE_HINTS = (
    "carton", "boite", "box", "rangement", "range", "rangee",
    "sorti", "sors", "sortir", "ressorti", "pris", "prends", "prend",
    "emporte", "emprunt", "prete", "rendu", "remis", "remet", "rapporte",
    "retrouve", "cherche", "ou est", "ou sont", "ou se trouve",
)

STORAGE_ACTIONS = {"take_out": "out", "put_back": "in"}


def _box_labels() -> list[str]:
    """« 2 · Nautique » — ce que le modèle voit pour résoudre « le carton du nautique »."""
    return [f"{b['code']} · {b['name']}" if b["name"] else b["code"] for b in storage_list_boxes()]


def _storage_owner_names() -> list[str]:
    return [o["name"] for o in storage_summary()["owners"]]


def _match_box(hint: str) -> dict | None:
    """« 2 », « carton 2 », « nautique » → le carton correspondant, s'il est unique."""
    needle = storage_normalize(hint or "")
    for word in ("carton", "boite", "box"):
        needle = needle.replace(word, "")
    needle = needle.strip(" #.")
    if not needle:
        return None
    boxes = storage_list_boxes()
    for box in boxes:
        if needle in (storage_normalize(box["code"]), storage_normalize(box["name"])):
            return box
    partial = [b for b in boxes if b["name"] and needle in storage_normalize(b["name"])]
    return partial[0] if len(partial) == 1 else None


def _where(item: dict) -> str:
    return f"carton {item['box_code']}" if item["box_code"] else "sans carton"


def _storage_line(item: dict) -> str:
    bits = [item["name"], _where(item)]
    if item["owners"]:
        bits.append("/".join(item["owners"]))
    return " — ".join(bits)


def _apply_storage(item_id: int, action: str, note: str) -> str:
    if action == "out":
        item = storage_take_out(item_id, OutIn(note=note))
        return f"🚪 Sorti : {_storage_line(item)}" + (f" · {note}" if note else "")
    item = storage_put_back(item_id, InIn())
    return f"📦 Rangé : {_storage_line(item)}"


def _storage_candidates(name: str, box: dict | None, action: str) -> tuple[list[dict], list[dict]]:
    """(objets sur lesquels l'action a un sens, tous les objets trouvés)."""
    wanted = "stored" if action == "out" else "out"
    found = storage_list_items(q=name, box_id=box["id"] if box else None)
    if not found and box:
        found = storage_list_items(q=name)      # le carton cité était peut-être le mauvais
    return [i for i in found if i["status"] == wanted], found


def _storage_step(entry: dict, box: dict | None, action: str, note: str) -> tuple:
    """→ ('done', ligne) ou ('ask', question, [ids parmi lesquels choisir])."""
    name = entry["name"]
    usable, found = _storage_candidates(name, box, action)
    if not found:
        return "done", f"❓ Je ne trouve pas « {name} » dans l'inventaire."
    if not usable:
        item = found[0]
        state = "déjà sorti" if action == "out" else "déjà rangé"
        return "done", f"↩️ « {item['name']} » est {state} ({_where(item)})."
    if len(usable) == 1:
        return "done", _apply_storage(usable[0]["id"], action, note)

    lines = [f"Plusieurs objets correspondent à « {name} » :"]
    lines += [f"{n}. {_storage_line(i)}" for n, i in enumerate(usable, 1)]
    lines.append("Réponds avec le numéro (ou « annuler »).")
    return "ask", "\n".join(lines), [i["id"] for i in usable]


def _storage_run(queue: list, box: dict | None, action: str, note: str,
                 chat_id: int, done: list | None = None) -> str:
    """Traite les objets un par un, en s'arrêtant dès qu'il faut lever une ambiguïté."""
    done = list(done or [])
    while queue:
        result = _storage_step(queue.pop(0), box, action, note)
        if result[0] == "done":
            done.append(result[1])
            continue
        _remember(chat_id, stage="storage_pick", action=action, note=note,
                  choices=result[2], queue=queue, box=box, done=done)
        return "\n".join(done + ["", result[1]]).strip()
    _forget(chat_id)
    return "\n".join(done)


def _storage_pick(text: str, chat_id: int, state: dict) -> str:
    """Réponse à un « lequel ? » : un numéro dans la liste proposée."""
    answer = text.strip().rstrip(".")
    if not answer.isdigit():
        return "Réponds-moi avec le numéro de l'objet (ou « annuler »)."
    index = int(answer) - 1
    if not 0 <= index < len(state["choices"]):
        return f"Il n'y a pas de {answer} dans la liste."
    line = _apply_storage(state["choices"][index], state["action"], state["note"])
    return _storage_run(state["queue"], state["box"], state["action"], state["note"],
                        chat_id, state["done"] + [line])


def _storage_add(items: list[dict], box: dict | None, chat_id: int) -> str:
    if box is None:
        _remember(chat_id, stage="storage_box", items=items)
        recap = ", ".join(i["name"] for i in items)
        return f"Noté : {recap}.\nDans quel carton ? (son numéro ou son nom)"
    created = [storage_create_item(ItemIn(box_id=box["id"], **item)) for item in items]
    _forget(chat_id)
    label = f"carton {box['code']}" + (f" ({box['name']})" if box["name"] else "")
    return "\n".join([f"📥 Ajouté au {label} :"] + [f"• {c['name']}" for c in created])


def _storage_find(query: str) -> str:
    items = storage_list_items(q=query)
    if not items:
        return f"🔎 Rien trouvé pour « {query} »."
    lines = [f"🔎 « {query} » :"]
    for item in items[:10]:
        suffix = ""
        if item["status"] == "out":
            suffix = " · 🚪 sorti" + (f" ({item['out_note']})" if item["out_note"] else "")
        lines.append(f"• {_storage_line(item)}{suffix}")
    if len(items) > 10:
        lines.append(f"… et {len(items) - 10} autre(s)")
    return "\n".join(lines)


def _looks_like_storage(text: str) -> bool:
    haystack = storage_normalize(text)
    return any(storage_normalize(hint) in haystack for hint in STORAGE_HINTS)


def _start_storage(text: str, chat_id: int, forced: bool = False) -> str | None:
    """Analyse un message de rangement. `forced` = expliquer les échecs au lieu de se taire."""
    try:
        parsed = parse_storage_message(text, _box_labels(), _storage_owner_names())
    except LLMError as e:
        return f"Je n'ai pas réussi à analyser ça ({e})." if forced else None

    if parsed["intent"] == "other":
        return (
            "Je n'ai pas compris ce qu'il fallait mettre à jour. Essaie par exemple :\n"
            "« j'ai sorti le wetsuit long du carton 2, prêté à Tom »"
        ) if forced else None

    box = _match_box(parsed["box"] or "")
    if parsed["intent"] == "find":
        return _storage_find(parsed["query"])
    if parsed["intent"] == "add":
        return _storage_add(parsed["items"], box, chat_id)
    return _storage_run(list(parsed["items"]), box, STORAGE_ACTIONS[parsed["intent"]],
                        parsed["note"], chat_id)


def _storage_text_handler(text: str, chat_id: int, user_id: int, is_private: bool) -> str | None:
    """Messages simples. None = « pas pour moi », le handler suivant est essayé."""
    state = _pending_for(chat_id)
    if state:
        if state["stage"] not in ("storage_pick", "storage_box"):
            return None                     # une autre fonctionnalité mène la conversation
        if text.strip().casefold() in CANCEL_WORDS:
            _forget(chat_id)
            return "Ok, j'oublie ça."
        if state["stage"] == "storage_pick":
            return _storage_pick(text, chat_id, state)
        box = _match_box(text)
        if box is None:
            return f"Je ne trouve pas ce carton. Lequel ? ({', '.join(_box_labels()[:12])}…)"
        return _storage_add(state["items"], box, chat_id)

    # Hors conversation : en tête-à-tête seulement, et uniquement si le message
    # ressemble à du rangement (sinon on laisse la liste d'envies l'analyser).
    if not is_private or not _looks_like_storage(text):
        return None
    return _start_storage(text, chat_id)


def _storage_command(args: str, chat_id: int, user_id: int) -> str:
    """/cartons — l'inventaire en un coup d'œil, ou une recherche si un texte suit."""
    if args:
        return _storage_find(args)
    summary = storage_summary()
    lines = [f"📦 Rangement — {summary['total_items']} objets dans {summary['total_boxes']} cartons", ""]
    for box in storage_list_boxes():
        label = box["name"] or f"Carton {box['code']}"
        out = f", {box['out_count']} sorti(s)" if box["out_count"] else ""
        lines.append(f"• {box['code']} — {label} ({box['item_count']} objets{out})")
    if summary["out_count"]:
        lines += ["", f"🚪 {summary['out_count']} objet(s) hors carton — /sortis"]
    return "\n".join(lines)


def _out_items_command(args: str, chat_id: int, user_id: int) -> str:
    out = storage_summary()["out"]
    if not out:
        return "📦 Tout est rangé dans les cartons ✅"
    lines = ["🚪 Sortis des cartons", ""]
    for item in out:
        since = "depuis le " + item["out_since"][:10] if item["out_since"] else ""
        detail = " · ".join(d for d in (item["out_note"], since) if d)
        lines.append(f"• {_storage_line(item)}" + (f"\n   {detail}" if detail else ""))
    return "\n".join(lines)


def _where_command(args: str, chat_id: int, user_id: int) -> str:
    if not args:
        return "Qu'est-ce que tu cherches ? « /ou wetsuit »"
    return _storage_find(args)


def _take_out_command(args: str, chat_id: int, user_id: int) -> str:
    if not args:
        return "Dis-moi ce que tu sors : « /sorti le wetsuit long du carton 2, prêté à Tom »"
    return _start_storage(f"j'ai sorti {args}", chat_id, forced=True)


def _put_back_command(args: str, chat_id: int, user_id: int) -> str:
    if not args:
        return "Dis-moi ce que tu ranges : « /range le stéthoscope »"
    return _start_storage(f"j'ai rangé {args}", chat_id, forced=True)


def start() -> None:
    """Register commands + cron jobs and start scheduler/polling. Idempotent."""
    if not telegram.is_configured():
        return
    telegram.register_command("/plantes", _plants_command)
    telegram.register_command("/envies", _wishlist_command)
    telegram.register_command("/envie", _wish_add_command)
    telegram.register_command("/moi", _me_command)
    telegram.register_command("/cartons", _storage_command)
    telegram.register_command("/sortis", _out_items_command)
    telegram.register_command("/ou", _where_command)
    telegram.register_command("/sorti", _take_out_command)
    telegram.register_command("/range", _put_back_command)
    telegram.register_command("/series", _shows_command)
    telegram.register_text_handler(_storage_text_handler)
    telegram.register_text_handler(_wish_text_handler)
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
        _scheduler.add_job(
            _shows_sync_check,
            CronTrigger(minute=f"*/{SHOWS_SYNC_CHECK_MINUTES}", timezone=TELEGRAM_TZ or None),
            id="bot-shows-sync-check",
            next_run_time=datetime.now().astimezone(),  # rattrape aussi au démarrage
            coalesce=True,
            misfire_grace_time=None,  # un contrôle en retard (machine en veille) tourne quand même
        )
        _scheduler.start()
    telegram.start_polling()


def stop() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


@router.get("/status")
def bot_status():
    """What the bot can do right now — the dashboard uses it to warn about a missing key.

    `ai_configured` gates the plain-French commands only: the read-only ones
    (`/plantes`, `/envies`, `/cartons`, `/ou`, `/sortis`) never call an LLM.
    """
    return {
        "telegram_configured": telegram.is_configured(),
        "ai_configured": bool(OPENROUTER_API_KEY),
        "tmdb_configured": tmdb.is_configured(),
    }


@router.post("/watering-check", status_code=200)
def watering_check():
    """Trigger a watering check right now (same logic as the 09:00/21:00 cron)."""
    return {"sent": send_watering_check(), "configured": telegram.is_configured()}


@router.post("/shows-check", status_code=200)
def shows_check():
    """Trigger a show sync + new-episode check right now (same logic as the daily 08:00 sync)."""
    return {"sent": sync_and_notify_shows(), "configured": telegram.is_configured()}