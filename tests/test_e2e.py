"""E2E No-look MVP: coding -> validation -> judge PASS -> DONE."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_company.db.models import AuditEvent, Execution, JudgeDecision, Task, Worker
from ai_company.enums import TaskStatus, WorkerStatus
from ai_company.services.scheduler import run_once
from ai_company.services.tasks import create_task
from ai_company.services.validation import validation_status


def test_e2e_nolook_mock_pipeline(db_session: Session, tmp_env: Path) -> None:
    task = create_task(db_session, title="sample", priority=50, risk_level="low")
    assert task.status == TaskStatus.READY

    result = run_once(db_session, max_to_start=8)
    assert result.started >= 3

    db_session.refresh(task)
    assert task.status == TaskStatus.DONE

    workers = list(db_session.scalars(select(Worker)).all())
    assert len(workers) >= 3
    assert all(w.status == WorkerStatus.TERMINATED for w in workers)

    executions = list(db_session.scalars(select(Execution)).all())
    assert len(executions) >= 3

    events = {e.event_type for e in db_session.scalars(select(AuditEvent)).all()}
    for required in (
        "task_created",
        "task_ready",
        "validation_plan_created",
        "task_review_required",
        "validation_result_recorded",
        "judge_decision",
        "task_completed",
        "worker_terminated",
    ):
        assert required in events, f"missing audit event: {required}"

    decisions = list(db_session.scalars(select(JudgeDecision)).all())
    assert len(decisions) == 1
    assert decisions[0].verdict == "PASS"

    status = validation_status(db_session, task.id)
    assert status["task_status"] == TaskStatus.DONE
    assert status["judge"][0]["verdict"] == "PASS"

    children = list(
        db_session.scalars(select(Task).where(Task.parent_task_id == task.id)).all()
    )
    assert any(c.kind == "validation" for c in children)
    assert any(c.kind == "judge" for c in children)


def test_coding_cannot_self_approve(db_session: Session, tmp_env: Path) -> None:
    task = create_task(db_session, title="impl", priority=50, risk_level="low")
    run_once(db_session, max_to_start=1)
    db_session.refresh(task)
    assert task.status == TaskStatus.REVIEW_REQUIRED
    assert task.status != TaskStatus.DONE
