"""Finance database: its own SQLite file, kept out of git.

`data/finance/finance.db` + `data/finance/docs/` are listed in `.gitignore`.
The location can be moved with the `FINANCE_DATA_DIR` environment variable
(the test-suite points it at a temporary folder).

Amounts are integer centimes (`*_cents` columns). Settings are one JSON
document merged over `DEFAULT_SETTINGS`, so a new setting never needs a
migration and an old database keeps working.
"""
from __future__ import annotations

import copy
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from ..database import DATA_DIR

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- People of the household. Adults earn, pay taxes and build pensions;
-- children cost money, bring family allowances and change tax parts.
CREATE TABLE IF NOT EXISTS members (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT NOT NULL,
    emoji             TEXT NOT NULL DEFAULT '🙂',
    kind              TEXT NOT NULL DEFAULT 'adult',   -- adult | child
    birth_date        TEXT,                            -- ISO date (expected child: due date)
    expected          INTEGER NOT NULL DEFAULT 0,      -- 1 = not born yet
    color             TEXT NOT NULL DEFAULT '',
    retire_age_target INTEGER,                         -- wished stop-work age (NULL = solver decides)
    meta              TEXT NOT NULL DEFAULT '{}',      -- json, see model.py (AVS history, commute, LAMal…)
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Recurring income lines (salary, side income…). One adult can have several.
CREATE TABLE IF NOT EXISTS incomes (
    id                         INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id                  INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
    kind                       TEXT NOT NULL DEFAULT 'salary',  -- salary | side | rental | other
    label                      TEXT NOT NULL DEFAULT '',
    employer                   TEXT NOT NULL DEFAULT '',
    gross_annual_cents         INTEGER NOT NULL DEFAULT 0,      -- per year, 13th salary + regular bonus included
    net_annual_cents           INTEGER,                         -- as paid (payslip) — calibration only
    work_pct                   REAL NOT NULL DEFAULT 100,
    lpp_employee_annual_cents  INTEGER,                         -- NULL = legal minimum estimate
    lpp_employer_annual_cents  INTEGER,
    start_date                 TEXT,
    end_date                   TEXT,
    document_id                INTEGER,
    notes                      TEXT NOT NULL DEFAULT '',
    created_at                 TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at                 TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS accounts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    institution   TEXT NOT NULL DEFAULT '',
    kind          TEXT NOT NULL,                  -- see model.ACCOUNT_KINDS
    currency      TEXT NOT NULL DEFAULT 'CHF',
    owners        TEXT NOT NULL DEFAULT '{}',     -- json {member_id: share}; {} = shared equally by the adults
    iban          TEXT NOT NULL DEFAULT '',
    valuation     TEXT NOT NULL DEFAULT 'manual', -- manual (snapshots) | holdings (positions × prices)
    equity_pct    REAL,                           -- share in stocks; NULL = default for the kind
    include_in_fi INTEGER NOT NULL DEFAULT 1,
    archived      INTEGER NOT NULL DEFAULT 0,
    meta          TEXT NOT NULL DEFAULT '{}',     -- LPP plan, mortgage rate, property tax value…
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Balance history: one value per account and date (the monthly check-in).
CREATE TABLE IF NOT EXISTS snapshots (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id    INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    date          TEXT NOT NULL,
    balance_cents INTEGER NOT NULL,               -- in the account's currency
    source        TEXT NOT NULL DEFAULT 'manual', -- manual | import | auto | document
    note          TEXT NOT NULL DEFAULT '',
    document_id   INTEGER,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (account_id, date)
);

CREATE TABLE IF NOT EXISTS holdings (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id       INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    symbol           TEXT NOT NULL DEFAULT '',    -- Yahoo symbol: VT, CHSPI.SW, VWRL.SW…
    isin             TEXT NOT NULL DEFAULT '',
    name             TEXT NOT NULL,
    quantity         REAL NOT NULL DEFAULT 0,
    currency         TEXT NOT NULL DEFAULT 'CHF',
    asset_class      TEXT NOT NULL DEFAULT 'equity', -- equity | bonds | cash | real_estate | crypto | commodity
    cost_basis_cents INTEGER,                     -- total paid, in the holding's currency
    manual_price     REAL,                        -- used when no online price is available
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS transactions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id      INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    date            TEXT NOT NULL,
    amount_cents    INTEGER NOT NULL,             -- signed, account currency (+ in, - out)
    currency        TEXT NOT NULL DEFAULT 'CHF',
    description     TEXT NOT NULL DEFAULT '',
    counterparty    TEXT NOT NULL DEFAULT '',
    category        TEXT NOT NULL DEFAULT '',
    category_source TEXT NOT NULL DEFAULT '',     -- rule | builtin | llm | manual | import
    is_transfer     INTEGER NOT NULL DEFAULT 0,   -- between own accounts: never income/spending
    member_id       INTEGER,                      -- who it concerns (NULL = account owners)
    import_id       INTEGER,
    fingerprint     TEXT NOT NULL,                -- dedup key (see importers.fingerprint)
    note            TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (account_id, fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_tx_date ON transactions(date);
CREATE INDEX IF NOT EXISTS idx_tx_account_date ON transactions(account_id, date);

CREATE TABLE IF NOT EXISTS rules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern     TEXT NOT NULL UNIQUE,             -- normalised words that must all appear
    category    TEXT NOT NULL,
    is_transfer INTEGER NOT NULL DEFAULT 0,
    origin      TEXT NOT NULL DEFAULT 'user',     -- user | learned
    hits        INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Planned spending per category (onboarding estimate, then compared to actuals).
CREATE TABLE IF NOT EXISTS budget (
    category      TEXT PRIMARY KEY,
    monthly_cents INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS goals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL,
    kind         TEXT NOT NULL DEFAULT 'custom',  -- emergency | baby | home | travel | fire | coast | custom
    emoji        TEXT NOT NULL DEFAULT '🎯',
    target_cents INTEGER NOT NULL DEFAULT 0,
    target_date  TEXT,
    account_ids  TEXT NOT NULL DEFAULT '[]',      -- json; [] = all liquid accounts of the scope
    scope        TEXT NOT NULL DEFAULT 'household', -- household | member:<id>
    achieved_at  TEXT,
    notes        TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Life events and plans: birth, part-time, home purchase, marriage, move abroad…
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    label       TEXT NOT NULL DEFAULT '',
    member_id   INTEGER REFERENCES members(id) ON DELETE CASCADE,
    start_date  TEXT,
    end_date    TEXT,
    params      TEXT NOT NULL DEFAULT '{}',
    scenario_id INTEGER REFERENCES scenarios(id) ON DELETE CASCADE, -- NULL = the base plan
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS scenarios (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    overrides   TEXT NOT NULL DEFAULT '{}',       -- json: settings/assumptions overridden
    base_events TEXT NOT NULL DEFAULT 'keep',     -- keep | drop : keep the base plan's events?
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS documents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    filename    TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    mime        TEXT NOT NULL DEFAULT '',
    size        INTEGER NOT NULL DEFAULT 0,
    sha256      TEXT NOT NULL UNIQUE,
    doc_type    TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending | parsing | review | applied | error | ignored
    engine      TEXT NOT NULL DEFAULT '',         -- camt | csv:<profile> | llm:<provider/model>
    extracted   TEXT,                             -- json result shown in the review screen
    error       TEXT NOT NULL DEFAULT '',
    member_id   INTEGER,
    account_id  INTEGER,
    uploaded_at TEXT NOT NULL DEFAULT (datetime('now')),
    parsed_at   TEXT,
    applied_at  TEXT
);

CREATE TABLE IF NOT EXISTS imports (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id   INTEGER,
    account_id    INTEGER,
    rows_total    INTEGER NOT NULL DEFAULT 0,
    rows_new      INTEGER NOT NULL DEFAULT 0,
    rows_duplicate INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS prices (
    symbol   TEXT NOT NULL,
    date     TEXT NOT NULL,
    close    REAL NOT NULL,
    currency TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (symbol, date)
);

CREATE TABLE IF NOT EXISTS fx_rates (
    currency     TEXT NOT NULL,
    date         TEXT NOT NULL,
    chf_per_unit REAL NOT NULL,
    PRIMARY KEY (currency, date)
);

CREATE TABLE IF NOT EXISTS cpi (
    month   TEXT PRIMARY KEY,                     -- YYYY-MM
    yoy_pct REAL                                  -- year-on-year inflation, %
);

-- Gamification
CREATE TABLE IF NOT EXISTS xp_events (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    kind  TEXT NOT NULL,
    ref   TEXT NOT NULL DEFAULT '',
    xp    INTEGER NOT NULL,
    note  TEXT NOT NULL DEFAULT '',
    at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (kind, ref)
);

CREATE TABLE IF NOT EXISTS badges (
    code      TEXT PRIMARY KEY,
    earned_at TEXT NOT NULL DEFAULT (datetime('now')),
    detail    TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS checkins (
    month           TEXT PRIMARY KEY,             -- YYYY-MM
    done_at         TEXT NOT NULL DEFAULT (datetime('now')),
    net_worth_cents INTEGER,
    liquid_cents    INTEGER,
    note            TEXT NOT NULL DEFAULT ''
);

-- Real tax bills, to check the engine against reality (calibration).
CREATE TABLE IF NOT EXISTS tax_bills (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    member_id            INTEGER,                 -- NULL = joint (married) bill
    year                 INTEGER NOT NULL,
    taxable_income_cents INTEGER,                 -- revenu imposable (ICC)
    rate_income_cents    INTEGER,                 -- revenu déterminant pour le taux
    taxable_wealth_cents INTEGER,
    icc_cents            INTEGER,                 -- canton + commune, income + wealth
    ifd_cents            INTEGER,
    document_id          INTEGER,
    notes                TEXT NOT NULL DEFAULT '',
    created_at           TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# Everything the household can tune. Stored values are merged over these, key by key.
DEFAULT_SETTINGS: dict = {
    "onboarding_done": False,
    "canton": "VD",
    "commune": "Lausanne",
    "communal_coefficient": 78.5,          # % of the cantonal base tax (Lausanne 2025-2029)
    "civil_status": "unmarried",           # unmarried | married
    "marriage_year": None,                 # scenario: married from this year on
    "child_tax_split": 0.5,                # unmarried parents: share of each child given to the first adult
    "spending_split": "income",            # individual view: income | equal
    "currency": "CHF",
    "fire": {
        "style": "normal",                 # lean | normal | fat
        "spending_annual": None,           # FI spending, today's CHF (None = current spending)
        "lean_ratio": 0.8,
        "fat_ratio": 1.4,
        "target_age": None,                # common wish; NULL = earliest possible
    },
    "assumptions": {
        "inflation_pct": 1.0,              # long-run CPI (SNB target 0-2 %)
        "equity_real_return_pct": 4.5,     # expected (arithmetic) real return, global equities in CHF
        "equity_vol_pct": 16.0,
        "bond_real_return_pct": 0.5,
        "bond_vol_pct": 5.0,
        "cash_real_return_pct": -0.5,      # savings account after inflation
        "equity_bond_corr": 0.1,
        "dividend_yield_pct": 2.0,         # taxable distributions of the equity part
        "bond_yield_pct": 1.0,
        "salary_growth_real_pct": 1.0,
        "salary_growth_until_age": 50,
        "lpp_interest_nominal_pct": 1.75,  # credited by the pension fund (legal minimum 1.25 % in 2026)
        "pillar3a_equity_pct": 90,         # invested 3a (VIAC, finpension, frankly…)
        "vested_equity_pct": 60,           # vested benefits once you stop working
        "property_growth_real_pct": 0.5,
        "swr_pct": 3.5,                    # rule-of-thumb comparison only — the plan uses the simulation
        "horizon_age": 95,
        "mc_runs": 1000,
        "mc_seed": 42,
        "lpp_withdrawal": "capital",       # capital | rente | half
        "pillar3a_accounts": 3,            # staggered withdrawals = lower capital tax
        "pillar3a_withdraw_from_age": 60,
        "avs_start_age": 65,               # 63-70 (early: -6.8 %/year)
        "liquid_floor": 0,                 # keep at least this much (today's CHF)
        "employer_maternity_topup": True,  # employer pays 100 % during maternity leave
        "fi_spending_change_pct": 0,       # spending once free vs now (e.g. -10 = 10 % less)
    },
    "child_costs": {                       # monthly, today's CHF, per child (direct costs, LAMal included)
        "0-3": 900, "4-12": 1000, "13-17": 1200, "18-24": 1300,
        "childcare_monthly": 1500,         # crèche, ages 0-3 (Lausanne: depends on income)
        "afterschool_monthly": 400,        # accueil parascolaire, ages 4-12
        "independent_at": 25,
    },
    "llm": {
        "mode": "local_first",             # local_first | local_only | cloud_only
        "local_base_url": "",              # "" = LOCAL_LLM_BASE_URL env or Ollama default
        "local_model": "",                 # "" = LOCAL_LLM_MODEL env or default vision model
        "allow_cloud": True,               # fallback to OpenRouter when local fails
    },
    "online": {"prices": True, "fx": True, "cpi": True},
}


def data_dir() -> Path:
    override = os.environ.get("FINANCE_DATA_DIR")
    return Path(override) if override else DATA_DIR / "finance"


def db_path() -> Path:
    return data_dir() / "finance.db"


def docs_dir() -> Path:
    return data_dir() / "docs"


def init_db() -> None:
    data_dir().mkdir(parents=True, exist_ok=True)
    docs_dir().mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA)


@contextmanager
def get_conn():
    conn = sqlite3.connect(db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def rows(rows_) -> list[dict]:
    return [dict(r) for r in rows_]


def loads(text, default):
    if text in (None, ""):
        return copy.deepcopy(default)
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return copy.deepcopy(default)


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def get_settings() -> dict:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = 'household'").fetchone()
    stored = loads(row["value"], {}) if row else {}
    return _deep_merge(DEFAULT_SETTINGS, stored)


def save_settings(patch: dict) -> dict:
    """Merge `patch` into the stored settings (nested dicts merged key by key)."""
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = 'household'").fetchone()
        stored = loads(row["value"], {}) if row else {}
        merged = _deep_merge(stored, patch)
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('household', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (json.dumps(merged),),
        )
    return get_settings()


def merge_settings(base: dict, over: dict) -> dict:
    """Public deep merge (scenario overrides on top of the settings)."""
    return _deep_merge(base, over)
