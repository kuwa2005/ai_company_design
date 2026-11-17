"""Database engine and session helpers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError, ProgrammingError
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
        migrate_additive_columns(engine)
        _engine = engine
        _SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
    elif url == "sqlite:///:memory:" or "mode=memory" in url:
        # ephemeral — caller manages; still return engine
        pass
    else:
        _engine = engine
        _SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

    return engine


# Columns added after the baseline schema. ``create_all`` never alters an
# existing table, so DBs created by an older version need these added in place.
# Additive + nullable only (safe to run repeatedly, no data rewrite).
_ADDITIVE_COLUMNS: dict[str, tuple[tuple[str, str], ...]] = {
    "tasks": (
        ("department_id", "VARCHAR(64)"),
        ("section_id", "VARCHAR(64)"),
    ),
}


def _is_duplicate_column_error(exc: BaseException) -> bool:
    """True when another process added the column first.

    SQLite: OperationalError "duplicate column name: x".
    PostgreSQL: ProgrammingError (psycopg DuplicateColumn, SQLSTATE 42701)
    "column \"x\" of relation \"tasks\" already exists".
    """
    orig = getattr(exc, "orig", None)
    if getattr(orig, "pgcode", None) == "42701" or getattr(orig, "sqlstate", None) == "42701":
        return True
    msg = str(exc).lower()
    return "duplicate column" in msg or "already exists" in msg


def migrate_additive_columns(engine: Engine) -> list[str]:
    """Add missing nullable columns to existing tables; backfill 部/課.

    Returns the ``table.column`` names that *this call* added (empty when up
    to date or when the table does not exist yet — ``create_all`` creates it).

    Idempotent and safe under concurrent start-up:

    - each ALTER runs in its own transaction, and a "duplicate column" error
      (another process added it between our inspect and ALTER) is treated as
      success (M-2);
    - the 部/課 backfill runs whenever rows with ``department_id IS NULL``
      remain, not only when this call added the column, so DBs migrated by
      alembic or left half-migrated are filled too (M-1).
    """
    added: list[str] = []
    insp = inspect(engine)
    existing_tables = set(insp.get_table_names())
    for table, columns in _ADDITIVE_COLUMNS.items():
        if table not in existing_tables:
            continue
        have = {c["name"] for c in insp.get_columns(table)}
        for name, ddl_type in columns:
            if name in have:
                continue
            try:
                with engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl_type}"))
                added.append(f"{table}.{name}")
            except (OperationalError, ProgrammingError) as exc:
                if not _is_duplicate_column_error(exc):
                    raise
    if "tasks" in existing_tables:
        with engine.begin() as conn:
            conn.execute(
                text("CREATE INDEX IF NOT EXISTS ix_tasks_department_id ON tasks (department_id)")
            )
            backfill_task_org_units(conn)
    return added


def backfill_task_org_units(conn: object) -> int:
    """Fill 部/課 for tasks that have none yet. Returns the number of rows updated.

    Rows whose task is 部長直轄 keep ``section_id`` NULL but get a department,
    so they are not revisited. Cheap no-op when nothing is missing.
    """
    from ai_company.org import org_unit_ids_for_task

    rows = conn.execute(  # type: ignore[attr-defined]
        text("SELECT id, title, role_required, kind FROM tasks WHERE department_id IS NULL")
    ).all()
    for task_id, title, role, kind in rows:
        dept_id, section_id = org_unit_ids_for_task(title or "", role or "", kind)
        conn.execute(  # type: ignore[attr-defined]
            text(
                "UPDATE tasks SET department_id = :d, section_id = :s "
                "WHERE id = :i AND department_id IS NULL"
            ),
            {"d": dept_id, "s": section_id, "i": task_id},
        )
    return len(rows)


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
    migrate_additive_columns(engine)
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
