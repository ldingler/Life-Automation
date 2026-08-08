from __future__ import annotations

import os
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path, monkeypatch):
    """Every test gets its own DB and cache; no test reads the developer's .env."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite+pysqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("SMTP_HOST", "")
    monkeypatch.setenv("EMAIL_TO", "")
    monkeypatch.setenv("EBAY_CLIENT_ID", "")
    monkeypatch.setenv("REQUEST_DELAY_SECONDS", "0")
    monkeypatch.setenv("REQUEST_JITTER_SECONDS", "0")
    monkeypatch.setenv("RESPECT_ROBOTS_TXT", "false")
    os.environ.pop("NELLIS_BASE_URL", None)

    from nellis.config import get_settings
    from nellis.db import reset_engine

    get_settings.cache_clear()
    reset_engine()
    yield
    get_settings.cache_clear()
    reset_engine()


@pytest.fixture
def db():
    from nellis.db import init_db, session_scope

    init_db()
    with session_scope() as session:
        yield session


@pytest.fixture
def settings():
    from nellis.config import get_settings

    return get_settings()


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text()
