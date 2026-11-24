"""Finances / FIRE — the household's road to financial independence.

Everything money-related lives here, in its **own** SQLite file
(`data/finance/finance.db`) and its own document folder
(`data/finance/docs/`). Both are **gitignored**: the repository is public, and
salaries, balances and bank statements must never reach GitHub. Moving the data
to another machine goes through an encrypted backup (`backup.py`).

Layout
------
- `db.py`         connection, schema, settings (defaults merged with stored JSON)
- `money.py`      Swiss-aware amount/date parsing, centimes, 5-centime rounding
- `params.py`     official Swiss figures per year (AVS, LPP, 3a, IFD, Vaud) + sources
- `tax.py`        federal (IFD) + Vaud income, wealth and pension-capital taxes
- `pension.py`    AVS (1st pillar), LPP (2nd), 3a — contributions and pensions
- `model.py`      reads the database into a plain household model for the engine
- `simulate.py`   year-by-year projection, FI date solver, Monte Carlo
- `metrics.py`    net worth, cash flow, savings rate, freedom days, history
- `gamify.py`     XP, levels (altitude), badges, quests, streaks
- `categorize.py` transaction categories, rules, learning from corrections
- `importers/`    CAMT.053, CSV (bank / broker profiles), PDF & images via LLM, OCR (Tesseract)
- `documents.py`  upload → detect → parse → review → apply
- `anonymize.py`  mode private_cloud: mask names/addresses/numbers here, check, restore the cloud's answer
- `market.py`     prices (Yahoo/Stooq), FX (ECB), Swiss CPI (SNB) — cached
- `backup.py`     encrypted export / import of the finance data

All amounts are stored as integer **centimes** in the database and exposed as
francs (float, 2 decimals) by the API. All projections are in **real terms**
(today's francs): returns, salary growth and pension values are net of
inflation; tax scales are held constant in real terms, which is what the
federal and Vaud cold-progression compensation does every year.
"""
