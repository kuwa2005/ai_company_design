"""Migration paths for tasks.department_id / section_id (review H-1, M-1, M-2, M-3).

Covers: alembic 0002 on empty / old / app-migrated DBs, schema_version 3→4,
half-migrated DBs, a concurrent ALTER race, and meeting→task 部/課 agreement
for ``kind``-tagged proposals.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

import ai_company.db as db_mod
from ai_company.db import migrate_additive_columns
from ai_company.db.schema_version import SCHEMA_VERSION, SchemaVersion, ensure_schema_version
from ai_company.paths import repo_root
from alembic import command

OLD_TASKS_DDL = (
    "CREATE TABLE tasks (id VARCHAR(64) PRIMARY KEY, title VARCHAR(512), "
    "role_required VARCHAR(64), kind VARCHAR(32){extra})"
)
OLD_ROWS = (
    "INSERT INTO tasks (id, title, role_required, kind) VALUES "
    "('T1', 'E2E 確認', 'coding_worker', 'implementation'), "
    "('T2', '規約点検', 'compliance_auditor', 'compliance_audit')"
)
EXPECTED = {"T1": ("development", "verification"), "T2": ("legal", "compliance")}


def _old_db(path: Path, extra: str = "") -> Engine:
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as conn:
        conn.execute(text(OLD_TASKS_DDL.format(extra=extra)))
        conn.execute(text(OLD_ROWS))
    return engine


def _units(engine: Engine) -> dict[str, tuple[Any, Any]]:
    with engine.connect() as conn:
        return {
            r[0]: (r[1], r[2])
            for r in conn.execute(text("SELECT id, department_id, section_id FROM tasks"))
        }


def _cols(engine: Engine) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns("tasks")}


def _alembic(db: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    # env.py resolves the URL from AI_COMPANY_DB_PATH; no ini file → no logging reconfig.
    monkeypatch.setenv("AI_COMPANY_DB_PATH", str(db))
    cfg = Config()
    cfg.set_main_option("script_location", str(repo_root() / "alembic"))
    return cfg


# --- 1. alembic 0002 -------------------------------------------------------


def test_alembic_upgrade_on_empty_db_is_noop(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "empty.db"
    cfg = _alembic(db, monkeypatch)
    command.upgrade(cfg, "head")  # H-1: used to raise NoSuchTableError
    command.downgrade(cfg, "0001_baseline")
    engine = create_engine(f"sqlite:///{db}")
    assert "tasks" not in inspect(engine).get_table_names()
    engine.dispose()


def test_alembic_upgrade_on_old_db_adds_and_backfills(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "old.db"
    engine = _old_db(db)
    cfg = _alembic(db, monkeypatch)
    command.upgrade(cfg, "head")
    assert {"department_id", "section_id"} <= _cols(engine)
    assert _units(engine) == EXPECTED  # M-1: alembic path backfills too
    # The app migration afterwards is a no-op and keeps the data.
    assert migrate_additive_columns(engine) == []
    assert _units(engine) == EXPECTED

    command.downgrade(cfg, "0001_baseline")
    assert not ({"department_id", "section_id"} & _cols(engine))
    command.upgrade(cfg, "head")  # round-trip
    assert _units(engine) == EXPECTED
    engine.dispose()


def test_alembic_after_app_migration(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "app.db"
    engine = _old_db(db)
    assert migrate_additive_columns(engine) == ["tasks.department_id", "tasks.section_id"]
    cfg = _alembic(db, monkeypatch)
    command.upgrade(cfg, "head")  # columns + index already there
    assert _units(engine) == EXPECTED
    command.downgrade(cfg, "0001_baseline")
    assert not ({"department_id", "section_id"} & _cols(engine))
    engine.dispose()


def test_app_backfills_rows_left_null_by_previous_migration(tmp_path: Path) -> None:
    # M-1: columns exist but rows were never filled (e.g. old alembic 0002).
    engine = _old_db(
        tmp_path / "null.db", extra=", department_id VARCHAR(64), section_id VARCHAR(64)"
    )
    assert _units(engine) == {"T1": (None, None), "T2": (None, None)}
    assert migrate_additive_columns(engine) == []
    assert _units(engine) == EXPECTED
    engine.dispose()


# --- 2. schema_version 3 → 4 ----------------------------------------------


def test_schema_version_bumps_from_3_to_current(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'sv.db'}")
    SchemaVersion.__table__.create(engine)
    factory = sessionmaker(bind=engine)
    with factory() as session:
        session.add(SchemaVersion(version=3, application_version="0.1.0"))
        session.commit()
    with factory() as session:
        assert ensure_schema_version(session) == SCHEMA_VERSION == 4
        session.commit()
    with factory() as session:
        versions = sorted(v.version for v in session.query(SchemaVersion).all())
        assert versions == [3, 4]
        # second call does not add another row
        assert ensure_schema_version(session) == 4
        session.commit()
        assert session.query(SchemaVersion).count() == 2
    engine.dispose()


# --- 3. half-migrated DBs --------------------------------------------------


@pytest.mark.parametrize(
    ("extra", "expect_added"),
    [
        (", department_id VARCHAR(64)", ["tasks.section_id"]),
        (", section_id VARCHAR(64)", ["tasks.department_id"]),
    ],
)
def test_partial_migration_completes_and_backfills(
    tmp_path: Path, extra: str, expect_added: list[str]
) -> None:
    engine = _old_db(tmp_path / "partial.db", extra=extra)
    assert migrate_additive_columns(engine) == expect_added
    assert {"department_id", "section_id"} <= _cols(engine)
    assert _units(engine) == EXPECTED
    idx = {ix["name"] for ix in inspect(engine).get_indexes("tasks")}
    assert "ix_tasks_department_id" in idx
    engine.dispose()


# --- 4. concurrent start-up -----------------------------------------------


def test_concurrent_alter_race_is_tolerated(tmp_path: Path, monkeypatch) -> None:
    """Another process adds the columns between our inspect and ALTER (M-2)."""
    db = tmp_path / "race.db"
    engine = _old_db(db)

    class StaleInspector:
        """Snapshot taken before the other process migrated."""

        def __init__(self, cols: list[dict[str, Any]]) -> None:
            self._cols = cols

        def get_table_names(self) -> list[str]:
            return ["tasks"]

        def get_columns(self, _table: str) -> list[dict[str, Any]]:
            return self._cols

    stale = StaleInspector(inspect(engine).get_columns("tasks"))
    other = create_engine(f"sqlite:///{db}")
    assert migrate_additive_columns(other) == ["tasks.department_id", "tasks.section_id"]
    other.dispose()

    monkeypatch.setattr(db_mod, "inspect", lambda _engine: stale)
    added = migrate_additive_columns(engine)  # used to raise "duplicate column name"
    assert added == []
    assert _units(engine) == EXPECTED
    engine.dispose()


def test_postgres_style_duplicate_column_is_tolerated(tmp_path: Path, monkeypatch) -> None:
    """R-2: PostgreSQL reports DuplicateColumn as ProgrammingError (SQLSTATE 42701)."""
    from sqlalchemy import event
    from sqlalchemy.exc import ProgrammingError

    db = tmp_path / "pg_race.db"
    engine = _old_db(db)

    class StaleInspector:
        def __init__(self, cols: list[dict[str, Any]]) -> None:
            self._cols = cols

        def get_table_names(self) -> list[str]:
            return ["tasks"]

        def get_columns(self, _table: str) -> list[dict[str, Any]]:
            return self._cols

    stale = StaleInspector(inspect(engine).get_columns("tasks"))
    other = create_engine(f"sqlite:///{db}")
    migrate_additive_columns(other)  # the "other process" wins the race
    other.dispose()

    class PgDuplicateColumn(Exception):
        pgcode = "42701"

        def __str__(self) -> str:
            return 'column "department_id" of relation "tasks" already exists'

    @event.listens_for(engine, "before_cursor_execute")
    def _as_postgres(_conn, _cursor, statement, _params, _ctx, _many):  # type: ignore[no-untyped-def]
        if statement.startswith("ALTER TABLE"):
            raise ProgrammingError(statement, {}, PgDuplicateColumn())

    monkeypatch.setattr(db_mod, "inspect", lambda _engine: stale)
    assert migrate_additive_columns(engine) == []
    assert _units(engine) == EXPECTED
    engine.dispose()


def test_duplicate_column_detection_by_sqlstate() -> None:
    from sqlalchemy.exc import ProgrammingError

    class Orig(Exception):
        sqlstate = "42701"  # psycopg3 style, message localized

    assert db_mod._is_duplicate_column_error(ProgrammingError("ALTER", {}, Orig("列は既に存在")))

    class Other(Exception):
        pgcode = "42601"  # syntax_error

    assert not db_mod._is_duplicate_column_error(ProgrammingError("ALTER", {}, Other("syntax")))


def test_non_duplicate_alter_errors_still_raise(tmp_path: Path, monkeypatch) -> None:
    engine = _old_db(tmp_path / "bad.db")
    monkeypatch.setitem(db_mod._ADDITIVE_COLUMNS, "tasks", (("bad col", "NOT A TYPE ("),))
    with pytest.raises(Exception, match="(?i)syntax"):
        migrate_additive_columns(engine)
    engine.dispose()


# --- 7. kind-tagged proposals: minutes == stored 部/課 ---------------------


def test_kind_tagged_proposal_is_filed_where_meeting_decided(
    db_session: Session, tmp_env
) -> None:
    from ai_company.db.models import Task
    from ai_company.services.executive_meeting import convene_executive_meeting
    from ai_company.services.secretary import GoalPlan, TaskProposal, apply_plan

    plan = GoalPlan(
        project_name="kind check",
        goal="社内用のCLIツールを作る",
        tasks=[
            TaskProposal(title="API 実装", objective="x", role_required="coding_worker"),
            TaskProposal(
                title="API 実装の独立確認",
                objective="y",
                role_required="coding_worker",
                kind="validation",
            ),
            TaskProposal(title="品質方針", objective="z", role_required="qa_director"),
        ],
        staffing=[],
    )
    minutes = convene_executive_meeting(plan)
    decided = {a.task_title: a for a in minutes.assignments}
    # kind changes the 課: same title words, but validation → 検証課
    assert decided["API 実装"].section_id == "build"
    assert decided["API 実装の独立確認"].section_id == "verification"
    assert decided["品質方針"].section_id is None  # 部長直轄

    result = apply_plan(db_session, plan, minutes=minutes)
    for task in result.tasks:
        a = decided[task.title]
        assert (task.department_id, task.section_id) == (a.department_id, a.section_id)
        assert task.kind == a.kind

    # The stored unit follows the minutes even if the derivation rule differs.
    minutes.assignments[0].section_id = "design"
    again = apply_plan(db_session, plan, minutes=minutes)
    first = db_session.get(Task, again.tasks[0].id)
    assert first is not None and first.section_id == "design"
