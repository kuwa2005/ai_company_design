"""Secretary must never spawn workers / Cursor processes."""

from __future__ import annotations

import ast
from pathlib import Path

import ai_company.services.secretary as secretary_mod
from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_company.db.models import AuditEvent, StaffingRequest, Task, Worker
from ai_company.enums import TaskStatus
from ai_company.services.secretary import start_goal, understand_goal
from ai_company.services.scheduler import run_once


FORBIDDEN_IMPORTS = {
    "subprocess",
    "os.system",
    "ai_company.services.cursor_runner",
    "ai_company.services.workers",
    "ai_company.services.scheduler",
}


def test_secretary_module_has_no_spawn_imports() -> None:
    path = Path(secretary_mod.__file__).resolve()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
                imported.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            imported.add(mod)
            for alias in node.names:
                imported.add(f"{mod}.{alias.name}" if mod else alias.name)

    for banned in FORBIDDEN_IMPORTS:
        assert banned not in imported, f"Secretary must not import {banned}"
    assert "subprocess" not in imported
    source = path.read_text(encoding="utf-8")
    assert "subprocess" not in source
    assert "Popen" not in source
    assert "shell=True" not in source


def test_secretary_has_no_spawn_callable() -> None:
    for name in dir(secretary_mod):
        lower = name.lower()
        assert "spawn" not in lower
        assert "popen" not in lower
        assert name not in {"run_cursor_cli", "dispatch_worker", "run_once"}


def test_start_goal_creates_tasks_and_staffing_without_workers(
    db_session: Session, tmp_env: Path
) -> None:
    result = start_goal(db_session, "優秀なWeb勤怠を作って")
    assert result.project.goal == "優秀なWeb勤怠を作って"
    assert len(result.tasks) >= 3
    assert len(result.staffing) >= 1

    workers = list(db_session.scalars(select(Worker)).all())
    assert workers == []

    statuses = {t.status for t in result.tasks}
    assert TaskStatus.READY in statuses or TaskStatus.WAITING in statuses

    staff = list(db_session.scalars(select(StaffingRequest)).all())
    assert len(staff) == len(result.staffing)

    events = {e.event_type for e in db_session.scalars(select(AuditEvent)).all()}
    assert "staffing_requested" in events
    assert "secretary_plan_applied" in events
    assert "task_proposed" in events


def test_dependency_chain_from_secretary(db_session: Session, tmp_env: Path) -> None:
    plan = understand_goal("勤怠Webアプリを作る")
    assert any(t.depends_on_titles for t in plan.tasks)
    result = start_goal(db_session, "勤怠Webアプリを作る")
    waiting = [t for t in result.tasks if t.status == TaskStatus.WAITING]
    ready = [t for t in result.tasks if t.status == TaskStatus.READY]
    assert ready  # at least root task
    assert waiting  # dependents wait


def test_scheduler_not_secretary_dispatches(db_session: Session, tmp_env: Path) -> None:
    result = start_goal(db_session, "小さなツールを作る")
    assert list(db_session.scalars(select(Worker)).all()) == []

    # Only Scheduler path creates workers; allow coding + review
    sched = run_once(db_session, runner="mock", max_to_start=2)
    assert sched.started >= 1
    assert list(db_session.scalars(select(Worker)).all())
    tasks = list(db_session.scalars(select(Task)).all())
    assert any(t.status in {TaskStatus.DONE, TaskStatus.REVIEW} for t in tasks)
