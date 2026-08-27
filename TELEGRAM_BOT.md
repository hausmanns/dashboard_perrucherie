# 🤖 Bot Telegram

Envoie un résumé d'arrosage dans un chat Telegram à **09h00 et 21h00**
(heure configurable), répond à `/plantes` à la demande, et tient à jour les
listes d'envies et l'inventaire des cartons. Le module est
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
- `/envies [prénom]` — listes d'envies de la maison (sans prénom : tout le
  monde). Le salon Telegram étant partagé, la réponse ne dit jamais **qui**
  s'occupe d'un cadeau : une envie déjà prise affiche seulement
  « 🔒 pris en charge ».
- `/envie <texte libre>` — **ajouter une envie en langage naturel**, par ex.
  « /envie un casque Sony vers 350.- chez Digitec pour Lea ». Le bot comprend
  le message, crée l'envie, puis **demande ce qui manque** (pour qui, le prix,
  un lien…) une question à la fois. Répondez normalement, ou « non » pour
  laisser en l'état.
- `/moi <prénom>` — associe votre compte Telegram à une personne. Ensuite,
  « je veux… » atterrit directement sur votre liste sans qu'on vous le demande.
- `/cartons [recherche]` — inventaire des cartons de rangement, ou recherche
  d'un objet si un texte suit.
- `/ou <objet>` — dans quel carton se trouve un objet (et s'il en est sorti).
- `/sortis` — tout ce qui est actuellement hors carton, avec depuis quand et
  chez qui.
- `/sorti <texte libre>` — **noter une sortie**, par ex.
  « /sorti le wetsuit long du carton 2, prêté à Tom ». La date est estampillée
  toute seule.
- `/range <texte libre>` — l'inverse : « /range le stéthoscope ».
- `/help` — liste des commandes

### En message privé, la commande est facultative

Dans une conversation privée avec le bot, écrivez simplement votre envie :

```
vous > j'aimerais offrir un pull en laine à Lea
bot  > 🎁 Ajouté à la liste de Léa :
       • Pull en laine

       Pour « Pull en laine », il me manque le prix, un lien ou le magasin
       et la taille. Réponds-moi, ou « non » pour laisser comme ça.
vous > environ 80 francs chez Zara, taille M
bot  > ✅ • Pull en laine · 80 CHF · Zara · taille M
```

Le rangement fonctionne pareil — dites simplement ce que vous avez pris ou
remis :

```
vous > j'ai sorti le stéthoscope du carton 18, c'est pour Fetsuko
bot  > 🚪 Sorti : Stetoscope — carton 18 — Lea · pour Fetsuko

vous > j'ai pris les lunettes de kitesurf
bot  > Plusieurs objets correspondent à « Lunettes kitesurf » :
       1. Lunettes kitesurf — carton 2 — Seb
       2. Lunettes kitesurf — carton 2 — Lea
       Réponds avec le numéro (ou « annuler »).
vous > 2
bot  > 🚪 Sorti : Lunettes kitesurf — carton 2 — Lea
```

Un même message peut concerner plusieurs objets, et « j'ai mis X dans le
carton Y » crée l'objet s'il n'existait pas encore (le bot demande le carton
si vous ne le précisez pas).

Les prénoms sont reconnus sans se soucier des accents ni de la casse :
`lea`, `Léa`, `LEA` désignent la même personne, comme `seb`, `Séb` et `SEB`.

**Dans un groupe**, le bot n'analyse pas la conversation : il faut `/envie …`.
C'est volontaire — sinon chaque message du salon partirait vers le LLM.

> ⚠️ Les commandes en langage naturel (`/envie`, `/sorti`, `/range`, et
> l'écriture libre en privé) ont besoin de `OPENROUTER_API_KEY` dans `.env`
> — la même clé que l'identification par photo. Sans elle, elles répondent que
> la clé manque ; tout le reste du bot fonctionne, y compris les commandes de
> lecture `/plantes`, `/envies`, `/cartons`, `/ou` et `/sortis`, qui ne
> touchent que la base.

## Comment ça marche

| Fichier | Rôle |
|---|---|
| `app/telegram.py` | Module générique : envoi de message, commandes, polling |
| `app/bot.py` | Branchement des fonctionnalités (rappel arrosage, `/plantes`, `/envies`, `/envie`, `/moi`, `/cartons`, `/ou`, `/sortis`, `/sorti`, `/range`) + job cron 09h/21h |
| `app/nlu.py` | Lecture d'un message libre → envie ou action de rangement structurée (via OpenRouter) |
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
