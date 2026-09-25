"""Database engine and session helpers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from ai_company.db.models import Base
from ai_company.paths import data_dir, db_path

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def get_engine(url: str | None = None, *, echo: bool = False) -> Engine:
    global _engine, _SessionLocal
    if _engine is not None and url is None:
        return _engine

    if url is None:
        data_dir().mkdir(parents=True, exist_ok=True)
        path = db_path()
        url = f"sqlite:///{path}"

    engine = create_engine(
        url,
        echo=echo,
        future=True,
        connect_args={"timeout": 60} if url.startswith("sqlite") else {},
    )

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_conn: object, _connection_record: object) -> None:
        cursor = dbapi_conn.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        if url.startswith("sqlite") and "mode=memory" not in url:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=60000")
        cursor.close()

    if url is None or url.startswith("sqlite"):
        # only assign globals for default engine
        pass

    if url.startswith("sqlite:///") and "mode=memory" not in url:
        _engine = engine
        _SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    elif url == "sqlite:///:memory:" or "mode=memory" in url:
        # ephemeral — caller manages; still return engine
        pass
    else:
        _engine = engine
        _SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    return engine


def reset_engine() -> None:
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None


def init_db(url: str | None = None) -> Engine:
    # Ensure SchemaVersion is registered on Base.metadata
    from ai_company.db import schema_version as _schema_version  # noqa: F401

    engine = get_engine(url)
    Base.metadata.create_all(engine)
    try:
        from ai_company.db.schema_version import ensure_schema_version

        factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
        with factory() as session:
            ensure_schema_version(session)
            session.commit()
    except Exception:
        pass
    return engine


@contextmanager
def session_scope(engine: Engine | None = None) -> Iterator[Session]:
    if engine is not None:
        factory = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
        session = factory()
    else:
        if _SessionLocal is None:
            get_engine()
        assert _SessionLocal is not None
        session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Session:
    if _SessionLocal is None:
        get_engine()
    assert _SessionLocal is not None
    return _SessionLocal()
