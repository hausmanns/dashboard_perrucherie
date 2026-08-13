# 🤖 Bot Telegram

Envoie un résumé d'arrosage dans un chat Telegram à **09h00 et 21h00**
(heure configurable), et répond à `/plantes` à la demande. Le module est
générique (`app/telegram.py`) : d'autres fonctionnalités du dashboard
peuvent se brancher dessus plus tard.

## 1. Créer le bot

1. Sur Telegram, ouvrez **@BotFather** et envoyez `/newbot`.
2. Donnez un nom et un username (terminé par `bot`, ex. `perrucherie_bot`).
3. Copiez le **token** fourni (format `123456789:AA...`).

## 2. Récupérer le chat_id

Envoie un premier message à votre bot (n'importe quoi), puis :

```bash
curl "https://api.telegram.org/bot<TOKEN>/getUpdates"
```

Dans la réponse, cherchez `"chat":{"id":123456789}` → c'est votre `TELEGRAM_CHAT_ID`.

## 3. Configurer

Éditez le fichier `.env` à la racine du repo :

```bash
TELEGRAM_BOT_TOKEN=123456789:AA...
TELEGRAM_CHAT_ID=123456789
TELEGRAM_TZ=Europe/Paris        # optionnel, sinon heure serveur (Docker = UTC)
```

`TELEGRAM_TZ` suit le format IANA. Exemples : `Europe/Paris`, `America/Toronto`.

## 4. Redémarrer

```bash
docker compose up -d --build
```

## 5. Vérifier

Tester l'envoi immédiatement (ne change pas la base) :

```bash
curl -X POST http://localhost:8000/api/bot/watering-check
# → {"sent": true, "configured": true}
```

Puis envoyez `/plantes` au bot dans Telegram : il répond avec l'état
d'arrosage actuel. Les rappels automatiques arrivent à 09h00 et 21h00.

## Inviter quelqu'un d'autre

Deux cas :

- **Pour qu'il puisse utiliser le bot lui-même** (commande `/plantes`) :
  envoyez-lui le @username du bot — dans Telegram il le cherche, appuie sur
  **Start** et peut l'utiliser directement.
- **Pour qu'il reçoive aussi les rappels 09h/21h** : le bot ne peut pousser
  de messages que vers des chats où il a été ajouté. Comme `TELEGRAM_CHAT_ID`
  ne contient qu'**un seul** chat, le plus simple pour plusieurs destinataires
  est un **groupe** :
  1. Créez un groupe Telegram, ajoutez-y le bot (cherchez son @username) et
     les personnes à notifier.
  2. Écrivez un message dans le groupe (pour que le bot le voie).
  3. Récupérez l'id du groupe : re-lancez `getUpdates`, ou écrivez à
     **@userinfobot** dans le groupe — il répond avec l'id du groupe (un
     nombre **négatif** type `-1001234567890`).
  4. Mettez cet id dans `.env` comme `TELEGRAM_CHAT_ID`, puis :
     ```bash
     docker compose up -d --build
     ```
  ⚠️ Par défaut un bot ne peut pas lire les messages d'un groupe : chez
  @BotFather, `/setprivacy` → **Disable** pour qu'il voie les messages et que
  ses rappels/commandes fonctionnent dans le groupe.

## Commandes du bot

- `/plantes` — état d'arrosage (plantes à arroser, sinon « toutes arrosées »)
- `/help` — liste des commandes

## Comment ça marche

| Fichier | Rôle |
|---|---|
| `app/telegram.py` | Module générique : envoi de message, commandes, polling |
| `app/bot.py` | Branchement des fonctionnalités (rappel arrosage + `/plantes`) + job cron 09h/21h |
| `POST /api/bot/watering-check` | Endpoint de test/trigger manuel |

Le « cron » est un `BackgroundScheduler` (APScheduler) qui tourne **dans le
même process que l'app** : il s'arrête et redémarre avec le conteneur. Pas de
LLM impliqué — juste une lecture de `GET /api/plants/due`.

## Ajouter une future fonctionnalité

Dans `app/bot.py` :

```python
def mon_rappel():
    telegram.send_message("Message programmé")

add_cron_job(mon_rappel, hour=8, minute=30)   # tous les jours à 08:30
telegram.register_command("/ma-commande", handler)  # commande interactive
```
