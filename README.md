# 🦜 Dashboard de la Perrucherie

Dashboard de gestion de la maison, auto-hébergé sur votre réseau local :

- 🌿 **Plantes** — suivi des plantes, fréquences d'arrosage, alertes quand il
  est temps d'arroser, historique d'arrosage, notifications navigateur.
- 🍽️ **Meal prep** — bibliothèque de plats + planning de repas sur plusieurs
  semaines (nombre de semaines et de repas par jour configurables).

Pensé « agent-first » : toutes les données sont lisibles et modifiables via une
API REST documentée (voir `AGENTS.md` et `/docs`).

## Lancer

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
git clone <repo> && cd dashboard_perrucherie && ./run.sh
```

Un simple `git pull` ailleurs récupère code **et** données.

## API pour agents

- `GET /api/summary` — vue d'ensemble (plantes à arroser, repas du jour, compteurs)
- `GET /docs` — documentation interactive Swagger
- `GET /openapi.json` — spec OpenAPI complète

Voir `AGENTS.md` pour la liste complète des endpoints et les conventions.

## Stack

FastAPI · SQLite · HTML/CSS/JS vanilla (aucun build). Python 3.10+ requis.
