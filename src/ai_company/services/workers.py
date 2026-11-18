"""Worker lifecycle: Mock Runner and Cursor Runner via Worker Manager."""

from __future__ import annotations

from typing import Literal

from sqlalchemy.orm import Session

from ai_company.config import AppConfig, load_config
from ai_company.db.models import Execution, Task, Worker
from ai_company.enums import WorkerStatus
from ai_company.ids import new_id
from ai_company.services.audit import record_audit
from ai_company.services.validation import (
    is_judge_task,
    is_validation_task,
)
from ai_company.services.worker_cursor import (
    run_cursor_worker,
)
from ai_company.services.worker_finalize import (
    _GUIDANCE_CHARS,
    _finalize_execution,
    _is_closure,
    _mark_starting,
    _reopen_read_only_parent,
    _utcnow,
    failure_cause,
    terminate_worker,
)
from ai_company.services.worker_mock import (
    mock_department_work_summary,
    run_mock_worker,
)

# Worker lifecycle is split into worker_finalize / worker_mock / worker_cursor;
# the names are re-exported here so existing imports (and monkeypatches of
# ``workers.run_mock_worker``) keep working.
__all__ = [
    "dispatch_worker",
    "failure_cause",
    "hire_worker",
    "list_workers",
    "mock_department_work_summary",
    "run_cursor_worker",
    "run_mock_worker",
    "RunnerKind",
    "terminate_worker",
    "_coding_worker_id_for_task",
    "_finalize_execution",
    "_GUIDANCE_CHARS",
    "_is_closure",
    "_mark_starting",
    "_reopen_read_only_parent",
    "_utcnow",
]

RunnerKind = Literal["mock", "cursor"]


def hire_worker(
    session: Session,
    *,
    task: Task,
    reuse_worker_id: str | None = None,
    config: AppConfig | None = None,
) -> Worker:
    """Hire a new ephemeral worker. Reusing a coding worker for validation is forbidden."""
    cfg = config or load_config()
    if reuse_worker_id and (
        is_validation_task(task) or is_judge_task(task)
    ):
        if cfg.validation.require_independent_execution:
            coding_worker_id = _coding_worker_id_for_task(session, task)
            if coding_worker_id and reuse_worker_id == coding_worker_id:
                record_audit(
                    session,
                    event_type="permission_denied",
                    actor_type="system",
                    actor_id="worker_manager",
                    target_type="task",
                    target_id=task.id,
                    metadata={
                        "reason": "reviewer_independence_violation",
                        "reuse_worker_id": reuse_worker_id,
                        "coding_worker_id": coding_worker_id,
                    },
                )
                raise PermissionError(
                    "Reviewer cannot reuse Coding Worker identity "
                    f"({reuse_worker_id})"
                )

    worker = Worker(
        id=new_id("WORKER"),
        role=task.role_required,
        project_id=task.project_id,
        task_id=task.id,
        status=WorkerStatus.HIRED,
        cost_estimated=task.estimated_cost,
    )
    session.add(worker)
    session.flush()
    record_audit(
        session,
        event_type="worker_hired",
        actor_type="system",
        actor_id="worker_manager",
        target_type="worker",
        target_id=worker.id,
        metadata={"task_id": task.id, "role": worker.role, "kind": task.kind},
    )
    return worker


def _coding_worker_id_for_task(session: Session, task: Task) -> str | None:
    if not task.source_execution_id:
        return None
    from ai_company.db.models import Execution

    execution = session.get(Execution, task.source_execution_id)
    if execution is None:
        return None
    return execution.worker_id



def dispatch_worker(
    session: Session,
    *,
    task: Task,
    worker: Worker,
    runner: RunnerKind = "mock",
    mock_succeed: bool = True,
    config: AppConfig | None = None,
    timeout_seconds: float | None = None,
) -> Execution:
    if runner == "cursor":
        return run_cursor_worker(
            session,
            task=task,
            worker=worker,
            config=config,
            timeout_seconds=timeout_seconds,
        )
    return run_mock_worker(session, task=task, worker=worker, succeed=mock_succeed)



def list_workers(session: Session) -> list[Worker]:
    from sqlalchemy import select

    return list(session.scalars(select(Worker).order_by(Worker.hired_at.desc())).all())
