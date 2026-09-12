"""Worker lifecycle and Mock Runner."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from ai_company.db.models import Execution, Task, Worker
from ai_company.enums import TaskStatus, WorkerStatus
from ai_company.ids import new_id
from ai_company.paths import executions_dir
from ai_company.services.audit import record_audit
from ai_company.services import cost_governor as cg
from ai_company.services.tasks import refresh_dependents


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def hire_worker(session: Session, *, task: Task) -> Worker:
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
        metadata={"task_id": task.id, "role": worker.role},
    )
    return worker


def run_mock_worker(
    session: Session,
    *,
    task: Task,
    worker: Worker,
    succeed: bool = True,
) -> Execution:
    """Synchronous mock runner: STARTING -> ACTIVE -> REPORTING -> TERMINATED."""
    worker.status = WorkerStatus.STARTING
    task.status = TaskStatus.RUNNING
    session.flush()
    record_audit(
        session,
        event_type="worker_started",
        actor_type="system",
        actor_id="worker_manager",
        target_type="worker",
        target_id=worker.id,
        metadata={"task_id": task.id},
    )
    record_audit(
        session,
        event_type="task_started",
        actor_type="worker",
        actor_id=worker.id,
        target_type="task",
        target_id=task.id,
        metadata={},
    )

    worker.status = WorkerStatus.ACTIVE
    session.flush()

    exec_id = new_id("EXEC")
    out_dir = executions_dir() / exec_id
    out_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = out_dir / "stdout.txt"
    stderr_path = out_dir / "stderr.txt"

    result: dict[str, Any]
    if succeed:
        result = {
            "status": "completed",
            "changed_files": [],
            "tests": {"passed": 1, "failed": 0},
            "commit": None,
            "issues": [],
            "mock": True,
            "task_id": task.id,
        }
        stdout_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        stderr_path.write_text("", encoding="utf-8")
        exit_code = 0
        success = True
        actual_cost = task.estimated_cost
    else:
        result = {
            "status": "failed",
            "changed_files": [],
            "tests": {"passed": 0, "failed": 1},
            "commit": None,
            "issues": ["mock failure"],
            "mock": True,
            "task_id": task.id,
        }
        stdout_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        stderr_path.write_text("mock failure\n", encoding="utf-8")
        exit_code = 1
        success = False
        actual_cost = task.estimated_cost * 0.5

    worker.status = WorkerStatus.REPORTING
    session.flush()

    execution = Execution(
        id=exec_id,
        task_id=task.id,
        worker_id=worker.id,
        started_at=_utcnow(),
        finished_at=_utcnow(),
        exit_code=exit_code,
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
        success=success,
        estimated_cost=task.estimated_cost,
        actual_cost=actual_cost,
        result_json=result,
    )
    session.add(execution)
    session.flush()

    terminate_worker(session, worker=worker, status=WorkerStatus.TERMINATED)

    if success:
        cg.spend_budget(session, task=task, amount=actual_cost)
        task.status = TaskStatus.DONE
        worker.cost_actual = actual_cost
        session.flush()
        record_audit(
            session,
            event_type="task_completed",
            actor_type="worker",
            actor_id=worker.id,
            target_type="task",
            target_id=task.id,
            metadata={"execution_id": execution.id},
        )
        refresh_dependents(session, task.id)
    else:
        cg.release_reservation(session, task=task, amount=task.estimated_cost)
        task.retry_count += 1
        worker.cost_actual = actual_cost
        if task.retry_count > task.max_retries:
            task.status = TaskStatus.BLOCKED
            record_audit(
                session,
                event_type="task_blocked",
                actor_type="system",
                actor_id="worker_manager",
                target_type="task",
                target_id=task.id,
                metadata={"retry_count": task.retry_count, "max_retries": task.max_retries},
            )
        else:
            task.status = TaskStatus.FAILED
            record_audit(
                session,
                event_type="task_failed",
                actor_type="worker",
                actor_id=worker.id,
                target_type="task",
                target_id=task.id,
                metadata={"retry_count": task.retry_count},
            )
            # requeue for retry
            task.status = TaskStatus.READY
            record_audit(
                session,
                event_type="task_ready",
                actor_type="system",
                actor_id="retry",
                target_type="task",
                target_id=task.id,
                metadata={"retry": True},
            )
        session.flush()
        cg.record_failure_and_maybe_trip(session)

    return execution


def terminate_worker(
    session: Session,
    *,
    worker: Worker,
    status: WorkerStatus = WorkerStatus.TERMINATED,
) -> None:
    worker.status = WorkerStatus.TERMINATING
    session.flush()
    worker.status = status
    worker.terminated_at = _utcnow()
    session.flush()
    record_audit(
        session,
        event_type="worker_terminated",
        actor_type="system",
        actor_id="worker_manager",
        target_type="worker",
        target_id=worker.id,
        metadata={"final_status": status},
    )


def list_workers(session: Session) -> list[Worker]:
    from sqlalchemy import select

    return list(session.scalars(select(Worker).order_by(Worker.hired_at.desc())).all())
