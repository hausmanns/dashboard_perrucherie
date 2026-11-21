"""Test-suite setup: the finance data lives in a throw-away folder, never in data/."""
import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_TMP = tempfile.mkdtemp(prefix="perrucherie-finance-tests-")
os.environ["FINANCE_DATA_DIR"] = _TMP
os.environ.setdefault("FINANCE_OFFLINE", "1")  # no network calls from the tests


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    """An empty finance database for one test."""
    monkeypatch.setenv("FINANCE_DATA_DIR", str(tmp_path))
    from app.finance import db
    db.init_db()
    return db
