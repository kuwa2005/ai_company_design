"""Worker lifecycle: Mock Runner and Cursor Runner via Worker Manager."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy.orm import Session

from ai_company.config import AppConfig, load_config
from ai_company.db.models import Execution, Task, Worker
from ai_company.enums import ReviewVerdict, TaskStatus, WorkerStatus
from ai_company.ids import new_id
from ai_company.paths import executions_dir, project_root
from ai_company.services import cost_governor as cg
from ai_company.services.audit import record_audit
from ai_company.services.cursor_runner import run_cursor_cli
from ai_company.services.escalation import escalate_blocked_task
from ai_company.services.progress import emit as progress
from ai_company.services.result_parser import parse_for_kind
from ai_company.services.validation import (
    finalize_judge,
    is_implementation_task,
    is_judge_task,
    is_validation_task,
    record_validation_result,
    start_validation_gate,
)

RunnerKind = Literal["mock", "cursor"]


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


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
        final_status = WorkerStatus.TERMINATED
    terminate_worker(session, worker=worker, status=final_status)

    actual_cost = execution.actual_cost if execution.actual_cost is not None else task.estimated_cost
    worker.cost_actual = actual_cost

    if success:
        cg.spend_budget(session, task=task, amount=float(actual_cost))

        # Judge: hard rules + decision; only Judge may mark parent DONE
        if is_judge_task(task):
            finalize_judge(session, judge_task=task, config=cfg)
            return execution

        # Validation reviewer: record evidence, mark this task DONE (not parent)
        if is_validation_task(task):
            payload = execution.result_json or {}
            verdict = str(payload.get("verdict") or payload.get("status") or "PASS").upper()
            if verdict in {"COMPLETED", "SUCCESS"}:
                verdict = ReviewVerdict.PASS
            if verdict not in {ReviewVerdict.PASS, ReviewVerdict.FAIL, ReviewVerdict.UNCERTAIN}:
                verdict = ReviewVerdict.PASS if success else ReviewVerdict.FAIL
            record_validation_result(
                session,
                validation_task=task,
                worker_id=worker.id,
                execution_id=execution.id,
                verdict=verdict,
                findings=list(payload.get("findings") or []),
                evidence=list(payload.get("evidence") or []),
                confidence=payload.get("confidence", 0.9),
            )
            task.status = TaskStatus.DONE
            session.flush()
            record_audit(
                session,
                event_type="task_completed",
                actor_type="worker",
                actor_id=worker.id,
                target_type="task",
                target_id=task.id,
                metadata={"execution_id": execution.id, "kind": "validation"},
            )
            from ai_company.services.tasks import refresh_dependents

            refresh_dependents(session, task.id)
            return execution

        # Implementation / repair: NEVER self-DONE when validation enabled
        if is_implementation_task(task):
            if task.kind == "repair":
                # Repair completion closes the repair task and opens a NEW validation plan
                # on the original parent — never reuse old ValidationResults for PASS.
                task.status = TaskStatus.DONE
                session.flush()
                record_audit(
                    session,
                    event_type="task_completed",
                    actor_type="worker",
                    actor_id=worker.id,
                    target_type="task",
                    target_id=task.id,
                    metadata={"execution_id": execution.id, "kind": "repair"},
                )
                parent = (
                    session.get(Task, task.parent_task_id) if task.parent_task_id else None
                )
                if parent is not None and cfg.validation.enabled:
                    start_validation_gate(
                        session,
                        parent=parent,
                        source_execution_id=execution.id,
                        config=cfg,
                    )
                return execution

            if cfg.validation.enabled and not cfg.validation.coding_worker_can_self_approve:
                start_validation_gate(
                    session,
                    parent=task,
                    source_execution_id=execution.id,
                    config=cfg,
                )
                return execution
            # Fail-closed: even if disabled, do not silent self-approve in production
            # paths; tests may set enabled=false for concurrency isolation only.
            task.status = TaskStatus.DONE
            session.flush()
            record_audit(
                session,
                event_type="task_completed",
                actor_type="worker",
                actor_id=worker.id,
                target_type="task",
                target_id=task.id,
                metadata={"execution_id": execution.id, "validation_disabled": True},
            )
            from ai_company.services.tasks import refresh_dependents

            refresh_dependents(session, task.id)
            return execution

        task.status = TaskStatus.DONE
        session.flush()
        return execution

    # Failure path
    cg.release_reservation(session, task=task, amount=task.estimated_cost)

    if is_validation_task(task):
        # Failed validation execution → FAIL verdict recorded, task DONE so Judge can run
        record_validation_result(
            session,
            validation_task=task,
            worker_id=worker.id,
            execution_id=execution.id,
            verdict=ReviewVerdict.FAIL,
            findings=[{"severity": "high", "category": "execution", "description": "validation worker failed"}],
            evidence=[],
            confidence=0.5,
        )
        task.status = TaskStatus.DONE
        session.flush()
        from ai_company.services.tasks import refresh_dependents

        refresh_dependents(session, task.id)
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
        if is_validation_task(task):
            result = {
                "status": "completed",
                "verdict": "PASS",
                "summary": f"mock {task.review_type} pass",
                "findings": [],
                "evidence": [{"kind": "mock"}],
                "confidence": 0.95,
                "mock": True,
                "task_id": task.id,
            }
        elif is_judge_task(task):
            result = {
                "status": "completed",
                "verdict": "PASS",
                "reason": "mock judge defers to hard rules",
                "mock": True,
                "task_id": task.id,
            }
        else:
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
        if is_validation_task(task):
            result = {
                "status": "failed",
                "verdict": "FAIL",
                "summary": f"mock {task.review_type} fail",
                "findings": [
                    {
                        "severity": "high",
                        "category": task.review_type or "review",
                        "description": "mock failure",
                        "evidence": [],
                        "recommended_fix": "repair",
                    }
                ],
                "evidence": [],
                "confidence": 0.7,
                "mock": True,
                "task_id": task.id,
            }
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

    timeout = timeout_seconds
    if timeout is None:
        timeout = float(cfg.cursor_runner.timeout_minutes) * 60.0
    progress(
        f"CURSOR invoke  {task.id}  「{task.title}」  "
        f"timeout={int(timeout)}s  workspace={cfg.cursor_runner.workspace or project_root()}"
    )

    capture = run_cursor_cli(
        task,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        config=cfg,
        timeout_seconds=timeout_seconds,
        cwd=cfg.cursor_runner.workspace or str(project_root()),
        session=session,
    )

    if is_validation_task(task):
        parse_kind = "reviewer"
    elif is_judge_task(task):
        parse_kind = "judge"
    else:
        parse_kind = "coding"

    parsed = parse_for_kind(capture.stdout, parse_kind)
    success = (
        not capture.timed_out
        and capture.exit_code == 0
        and parsed.ok
        and parsed.result is not None
    )
    progress(
        f"CURSOR return  {task.id}  exit={capture.exit_code}  "
        f"timed_out={capture.timed_out}  parse_ok={parsed.ok}  "
        f"parse_error={parsed.error}"
    )
    result_json: dict[str, Any] = parsed.result or {
        "status": "failed",
        "parse_error": parsed.error,
        "timed_out": capture.timed_out,
        "exit_code": capture.exit_code,
    }
    if capture.timed_out:
        result_json["timed_out"] = True
    result_json["parse_kind"] = parse_kind

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
            "parse_kind": parse_kind,
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
