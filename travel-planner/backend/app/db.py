"""SQLAlchemy engine/session plumbing.

Usage:
    with session_scope() as db: ...          # services, jobs, threads (commits on success)
    def endpoint(db: Session = Depends(get_db))  # FastAPI routers (caller commits)

The engine is created lazily from ``settings.database_url``; tests call
:func:`configure_engine` to point everything at a temporary database.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_settings

logger = logging.getLogger(__name__)

SQLITE_BUSY_TIMEOUT_MS = 30_000

_engine: Engine | None = None
_engine_lock = threading.Lock()


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


class _SessionFactory(sessionmaker[Session]):
    """``sessionmaker`` that binds to the lazily created engine on first use."""

    def __call__(self, **local_kw: Any) -> Session:
        if self.kw.get("bind") is None and "bind" not in local_kw:
            get_engine()
        return super().__call__(**local_kw)


SessionLocal: sessionmaker[Session] = _SessionFactory(autoflush=False, expire_on_commit=False)


def _set_sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


def _create_engine(url: str) -> Engine:
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite":
        return create_engine(url, pool_pre_ping=True)
    kwargs: dict[str, Any] = {
        "connect_args": {"check_same_thread": False, "timeout": SQLITE_BUSY_TIMEOUT_MS / 1000},
    }
    database = parsed.database
    if not database or database == ":memory:":
        kwargs["poolclass"] = StaticPool
    else:
        Path(database).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url, **kwargs)
    event.listen(engine, "connect", _set_sqlite_pragmas)
    return engine


def _configure_locked(url: str | None) -> Engine:
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = _create_engine(url or get_settings().database_url)
    SessionLocal.configure(bind=_engine)
    logger.debug("Database engine configured (%s)", _engine.url.get_backend_name())
    return _engine


def configure_engine(url: str | None = None) -> Engine:
    """(Re)create the global engine for ``url`` (default: settings) and rebind ``SessionLocal``."""
    with _engine_lock:
        return _configure_locked(url)


def get_engine() -> Engine:
    """Return the global engine, creating it from settings on first use."""
    engine = _engine
    if engine is None:
        with _engine_lock:
            engine = _engine if _engine is not None else _configure_locked(None)
    return engine


def init_db() -> None:
    """Create all tables that do not exist yet."""
    import app.models  # noqa: F401  (registers the models on Base.metadata)

    Base.metadata.create_all(get_engine())


@contextmanager
def session_scope() -> Iterator[Session]:
    """Transactional session: commits on success, rolls back on error, always closes."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a session; endpoints commit explicitly."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
