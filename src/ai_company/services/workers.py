"""Worker lifecycle: Mock Runner and Cursor Runner via Worker Manager."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy.orm import Session

from ai_company.config import AppConfig, load_config
from ai_company.db.models import Execution, Task, Worker
from ai_company.enums import TaskStatus, WorkerStatus
from ai_company.ids import new_id
from ai_company.paths import executions_dir
from ai_company.services import cost_governor as cg
from ai_company.services.audit import record_audit
from ai_company.services.cursor_runner import run_cursor_cli
from ai_company.services.escalation import escalate_blocked_task
from ai_company.services.result_parser import parse_structured_result
from ai_company.services.review import (
    complete_parent_after_review_pass,
    enqueue_review_task,
    handle_review_reject,
    role_requires_review,
)
from ai_company.services.tasks import refresh_dependents

RunnerKind = Literal["mock", "cursor"]


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


def _mark_starting(session: Session, *, task: Task, worker: Worker) -> None:
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


def _finalize_execution(
    session: Session,
    *,
    task: Task,
    worker: Worker,
    execution: Execution,
    success: bool,
    timed_out: bool = False,
    config: AppConfig | None = None,
) -> Execution:
    cfg = config or load_config()
    worker.status = WorkerStatus.REPORTING
    session.flush()

    final_status = WorkerStatus.TIMED_OUT if timed_out else WorkerStatus.TERMINATED
    if not success and not timed_out:
        # still terminate cleanly after failure
        final_status = WorkerStatus.TERMINATED
    terminate_worker(session, worker=worker, status=final_status)

    actual_cost = execution.actual_cost if execution.actual_cost is not None else task.estimated_cost
    worker.cost_actual = actual_cost

    if success:
        cg.spend_budget(session, task=task, amount=float(actual_cost))
        is_reviewer = not role_requires_review(task.role_required)

        if is_reviewer:
            task.status = TaskStatus.DONE
            session.flush()
            record_audit(
                session,
                event_type="task_completed",
                actor_type="worker",
                actor_id=worker.id,
                target_type="task",
                target_id=task.id,
                metadata={"execution_id": execution.id, "kind": "review"},
            )
            complete_parent_after_review_pass(session, review_task=task)
            return execution

        if cfg.settings.review.enabled:
            enqueue_review_task(session, parent=task, config=cfg)
            record_audit(
                session,
                event_type="task_awaiting_review",
                actor_type="worker",
                actor_id=worker.id,
                target_type="task",
                target_id=task.id,
                metadata={"execution_id": execution.id},
            )
            return execution

        task.status = TaskStatus.DONE
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
        return execution

    # Failure path
    cg.release_reservation(session, task=task, amount=task.estimated_cost)

    # Reviewer reject → rework parent instead of retrying the review forever
    if not role_requires_review(task.role_required) and task.parent_task_id:
        task.status = TaskStatus.DONE
        session.flush()
        record_audit(
            session,
            event_type="task_completed",
            actor_type="worker",
            actor_id=worker.id,
            target_type="task",
            target_id=task.id,
            metadata={"execution_id": execution.id, "review_verdict": "REJECT"},
        )
        handle_review_reject(session, review_task=task)
        cg.record_failure_and_maybe_trip(session)
        return execution

    task.retry_count += 1
    if timed_out:
        record_audit(
            session,
            event_type="task_failed",
            actor_type="worker",
            actor_id=worker.id,
            target_type="task",
            target_id=task.id,
            metadata={"reason": "timeout", "retry_count": task.retry_count},
        )
    if task.retry_count > task.max_retries:
        task.status = TaskStatus.BLOCKED
        session.flush()
        escalate_blocked_task(
            session,
            task=task,
            reason=f"retries_exhausted:{task.retry_count}/{task.max_retries}",
        )
    else:
        if not timed_out:
            record_audit(
                session,
                event_type="task_failed",
                actor_type="worker",
                actor_id=worker.id,
                target_type="task",
                target_id=task.id,
                metadata={"retry_count": task.retry_count},
            )
        task.status = TaskStatus.READY
        record_audit(
            session,
            event_type="task_ready",
            actor_type="system",
            actor_id="retry",
            target_type="task",
            target_id=task.id,
            metadata={"retry": True, "timed_out": timed_out},
        )
    session.flush()
    cg.record_failure_and_maybe_trip(session)
    return execution


def run_mock_worker(
    session: Session,
    *,
    task: Task,
    worker: Worker,
    succeed: bool = True,
) -> Execution:
    """Synchronous mock runner: STARTING -> ACTIVE -> REPORTING -> TERMINATED."""
    _mark_starting(session, task=task, worker=worker)

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
    return _finalize_execution(
        session, task=task, worker=worker, execution=execution, success=success
    )


def run_cursor_worker(
    session: Session,
    *,
    task: Task,
    worker: Worker,
    config: AppConfig | None = None,
    timeout_seconds: float | None = None,
) -> Execution:
    """Run task via Cursor CLI adapter. Fail closed on bad structured output."""
    cfg = config or load_config()
    _mark_starting(session, task=task, worker=worker)

    exec_id = new_id("EXEC")
    out_dir = executions_dir() / exec_id
    out_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = out_dir / "stdout.txt"
    stderr_path = out_dir / "stderr.txt"

    capture = run_cursor_cli(
        task,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        config=cfg,
        timeout_seconds=timeout_seconds,
        cwd=cfg.cursor_runner.workspace,
    )

    parsed = parse_structured_result(capture.stdout)
    success = (
        not capture.timed_out
        and capture.exit_code == 0
        and parsed.ok
        and parsed.result is not None
    )
    result_json: dict[str, Any] = parsed.result or {
        "status": "failed",
        "parse_error": parsed.error,
        "timed_out": capture.timed_out,
        "exit_code": capture.exit_code,
    }
    if capture.timed_out:
        result_json["timed_out"] = True

    actual_cost = task.estimated_cost if success else task.estimated_cost * 0.5
    execution = Execution(
        id=exec_id,
        task_id=task.id,
        worker_id=worker.id,
        started_at=_utcnow(),
        finished_at=_utcnow(),
        exit_code=capture.exit_code,
        stdout_path=str(stdout_path),
        stderr_path=str(stderr_path),
        success=success,
        estimated_cost=task.estimated_cost,
        actual_cost=actual_cost,
        result_json=result_json,
    )
    session.add(execution)
    session.flush()

    record_audit(
        session,
        event_type="cursor_runner_finished",
        actor_type="system",
        actor_id="cursor_runner",
        target_type="execution",
        target_id=execution.id,
        metadata={
            "success": success,
            "timed_out": capture.timed_out,
            "exit_code": capture.exit_code,
            "parse_error": parsed.error,
            "command0": capture.command[0] if capture.command else None,
        },
    )

    return _finalize_execution(
        session,
        task=task,
        worker=worker,
        execution=execution,
        success=success,
        timed_out=capture.timed_out,
        config=cfg,
    )


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
