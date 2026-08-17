"""Database engine and session management.

SQLite by default; the schema is deliberately Postgres-compatible (no SQLite-only
types) so `DATABASE_URL` can be repointed at Postgres without a migration.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings

log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


_engine = None
_SessionFactory = None


def get_engine():
    global _engine, _SessionFactory
    if _engine is None:
        settings = get_settings()
        url = settings.database_url
        if url.startswith("sqlite"):
            db_path = url.split("///")[-1]
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(url, echo=False, future=True)

        if url.startswith("sqlite"):
            # WAL keeps the scheduler writing while the dashboard reads.
            @event.listens_for(_engine, "connect")
            def _sqlite_pragmas(dbapi_conn, _record):
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.execute("PRAGMA busy_timeout=5000")
                cur.close()

        _SessionFactory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def get_session_factory():
    if _SessionFactory is None:
        get_engine()
    return _SessionFactory


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session. Commits on success, rolls back on error."""
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    from . import models  # noqa: F401  (registers mappers)

    engine = get_engine()
    Base.metadata.create_all(engine)
    _add_missing_columns(engine)


def _add_missing_columns(engine) -> int:
    """Add columns that exist in the models but not yet in the database.

    `create_all` creates missing *tables* and silently ignores missing
    *columns*, so a database created by an earlier version keeps working right
    up until something writes to a new field — and then fails with
    "table valuations has no column named market_verification", which tells the
    operator nothing useful about what to do next.

    This is deliberately not a migration framework. It only ever ADDs nullable
    columns; it never drops, renames or retypes anything, so it cannot lose
    data. Anything more complicated than an added column is a real migration and
    should be handled as one.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    added = 0

    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if not inspector.has_table(table.name):
                continue
            existing = {col["name"] for col in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing or column.primary_key:
                    continue
                if not column.nullable and column.default is None:
                    log.warning(
                        "column %s.%s is NOT NULL with no default — add it by hand",
                        table.name,
                        column.name,
                    )
                    continue
                ddl = column.type.compile(engine.dialect)
                conn.execute(
                    text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {ddl}')
                )
                log.info("added column %s.%s", table.name, column.name)
                added += 1

    return added


def reset_engine() -> None:
    """Test hook — drop cached engine so a new DATABASE_URL takes effect."""
    global _engine, _SessionFactory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionFactory = None
