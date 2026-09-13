"""E2E: Task create -> READY -> Scheduler -> review -> DONE -> TERMINATED -> Audit."""

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
    assert result.started >= 1

    db_session.refresh(task)
    assert task.status == TaskStatus.DONE

    workers = list(db_session.scalars(select(Worker)).all())
    assert len(workers) >= 1
    assert all(w.status == WorkerStatus.TERMINATED for w in workers)

    executions = list(db_session.scalars(select(Execution)).all())
    assert len(executions) >= 1
    assert any(e.success for e in executions)

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
        "task_review_enqueued",
    ):
        assert required in events, f"missing audit event: {required}"

    audit_jsonl = tmp_env / "data" / "audit.jsonl"
    assert audit_jsonl.exists()
    assert audit_jsonl.read_text(encoding="utf-8").strip()

    # review child exists and is DONE
    reviews = list(
        db_session.scalars(select(Task).where(Task.parent_task_id == task.id)).all()
    )
    assert len(reviews) == 1
    assert reviews[0].status == TaskStatus.DONE
    assert reviews[0].role_required == "reviewer"
