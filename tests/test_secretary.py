"""Secretary must never spawn workers / Cursor processes."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

import ai_company.services.secretary as secretary_mod
from ai_company.db.models import AuditEvent, StaffingRequest, Task, TaskDependency, Worker
from ai_company.enums import TaskStatus
from ai_company.services.scheduler import run_once
from ai_company.services.secretary import start_goal, understand_goal

FORBIDDEN_IMPORTS = {
    "subprocess",
    "os.system",
    "ai_company.services.cursor_runner",
    "ai_company.services.workers",
    "ai_company.services.scheduler",
}


# secretary.py was split; the ban covers every module that makes up the secretary.
SECRETARY_MODULES = (
    "secretary",
    "goal_planning",
    "delivery_state",
    "meeting_questions",
    "status_report",
    "next_actions",
)


@pytest.mark.parametrize("module", SECRETARY_MODULES)
def test_secretary_module_has_no_spawn_imports(module: str) -> None:
    path = Path(secretary_mod.__file__).resolve().parent / f"{module}.py"
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
    # 勤怠 → 法務部が出席し、最初の設計は法務事前点検（部長所見）を待つ
    assert not ready
    assert waiting  # dependents wait
    root = result.tasks[0]
    deps = {
        d.depends_on_task_id
        for d in db_session.scalars(
            select(TaskDependency).where(TaskDependency.task_id == root.id)
        ).all()
    }
    gates = {t.id for t in result.head_brief_tasks} | {t.id for t in result.work_order_tasks}
    assert deps and deps <= gates


def test_scheduler_not_secretary_dispatches(db_session: Session, tmp_env: Path) -> None:
    from ai_company.config import clear_config_cache
    from tests.conftest import write_validation_override

    write_validation_override(tmp_env, validation={"enabled": False})
    clear_config_cache()
    start_goal(db_session, "小さなツールを作る")
    assert list(db_session.scalars(select(Worker)).all()) == []

    sched = run_once(db_session, runner="mock", max_to_start=2)
    assert sched.started >= 1
    assert list(db_session.scalars(select(Worker)).all())
    tasks = list(db_session.scalars(select(Task)).all())
    assert any(t.status == TaskStatus.DONE for t in tasks)
