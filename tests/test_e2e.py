"""E2E: Task create -> READY -> Scheduler -> Budget ALLOW -> Worker -> DONE -> TERMINATED -> Audit."""

from __future__ import annotations

from pathlib import Path

from ai_company.db.models import AuditEvent, Execution, Task, Worker
from ai_company.enums import TaskStatus, WorkerStatus
from ai_company.services.scheduler import run_once
from ai_company.services.tasks import create_task
from sqlalchemy import select
from sqlalchemy.orm import Session


def test_e2e_mock_pipeline(db_session: Session, tmp_env: Path) -> None:
    task = create_task(db_session, title="sample", priority=50)
    assert task.status == TaskStatus.READY

    result = run_once(db_session)
    assert result.started == 1

    db_session.refresh(task)
    assert task.status == TaskStatus.DONE

    workers = list(db_session.scalars(select(Worker)).all())
    assert len(workers) == 1
    assert workers[0].status == WorkerStatus.TERMINATED
    assert workers[0].task_id == task.id

    executions = list(db_session.scalars(select(Execution)).all())
    assert len(executions) == 1
    assert executions[0].success is True
    assert executions[0].stdout_path
    assert Path(executions[0].stdout_path).exists()

    events = {
        e.event_type
        for e in db_session.scalars(select(AuditEvent)).all()
    }
    for required in (
        "task_created",
        "task_ready",
        "budget_reserved",
        "worker_hired",
        "worker_started",
        "task_started",
        "task_completed",
        "worker_terminated",
        "budget_spent",
    ):
        assert required in events, f"missing audit event: {required}"

    audit_jsonl = tmp_env / "data" / "audit.jsonl"
    assert audit_jsonl.exists()
    assert audit_jsonl.read_text(encoding="utf-8").strip()
