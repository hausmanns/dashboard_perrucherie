# 🦜 Dashboard de la Perrucherie

Dashboard de gestion de la maison, auto-hébergé sur votre réseau local :

- 🌿 **Plantes** — suivi des plantes, fréquences d'arrosage, alertes quand il
  est temps d'arroser, historique d'arrosage, notifications navigateur.
- 🍽️ **Meal prep** — bibliothèque de plats + planning de repas sur plusieurs
  semaines (nombre de semaines et de repas par jour configurables).
- 🛒 **Courses & frigo** — liste de courses manuelle ou générée depuis les
  ingrédients d'un plan de repas ; un article coché « acheté » part au frigo.
- 🎁 **Envies** — la liste d'envies de chaque habitant·e : prix, boutique,
  lien, taille, couleur, niveau d'envie, occasion, photo. Celui ou celle qui
  offre peut **réserver** une envie (personne n'achète deux fois le même
  cadeau), et la liste s'affiche par défaut **sans spoiler** : le/la
  destinataire ne voit ni qui a réservé, ni ce qui est déjà acheté.
  Une envie s'ajoute aussi **depuis Telegram en langage naturel** :
  « /envie un casque Sony vers 350.- pour Lea » — le bot demande ce qui manque.

Pensé « agent-first » : toutes les données sont lisibles et modifiables via une
API REST documentée (voir `AGENTS.md` et `/docs`).

## Lancer

### Avec Docker (recommandé — aucune dépendance locale)

```bash
docker compose up -d --build
```

Le dashboard tourne sur `http://localhost:8000` et `http://<ip-locale>:8000`
pour les autres appareils du réseau. La base SQLite reste un fichier normal
dans `./data` (monté dans le conteneur) : elle continue de voyager avec git.

Après un `git pull` sur une autre machine :

```bash
docker compose up -d --build   # rebuild + relance, données déjà là
```

### Sans Docker

```bash
./run.sh
```

Le script crée un environnement virtuel, installe les dépendances et affiche :

```
Local:   http://localhost:8000
Réseau:  http://192.168.x.x:8000   <-- depuis votre téléphone, tablette…
API doc: http://192.168.x.x:8000/docs
```

Ouvrez l'URL « Réseau » depuis n'importe quel appareil connecté au même Wi-Fi.

## Déplacer sur une autre machine

Tout est portable, **base de données incluse** (`data/dashboard.db` est versionnée) :

```bash
git clone <repo> && cd dashboard_perrucherie && docker compose up -d --build
```

(ou `./run.sh` si Docker n'est pas disponible — Python 3.10+ requis dans ce cas)

Un simple `git pull` ailleurs récupère code **et** données ; un
`docker compose up -d --build` applique les changements.

## API pour agents

- `GET /api/summary` — vue d'ensemble (plantes à arroser, repas du jour, anniversaires, compteurs)
- `GET /api/wishlist/overview` — « qu'est-ce que je pourrais offrir à X ? »
- `GET /docs` — documentation interactive Swagger
- `GET /openapi.json` — spec OpenAPI complète

Voir `AGENTS.md` pour la liste complète des endpoints et les conventions.

## Stack

FastAPI · SQLite · HTML/CSS/JS vanilla (aucun build). Python 3.10+ requis.
