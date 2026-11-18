"""Worker execution bookkeeping: start / finalize an execution and terminate the worker.

``_finalize_execution`` routes a finished execution by task kind (validation,
judge, closure audits, head briefs, attendance review, department work,
replan, implementation) and handles retries / escalation on failure."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from ai_company.config import AppConfig, load_config
from ai_company.db.models import Execution, Task, Worker
from ai_company.enums import ReviewVerdict, TaskKind, TaskStatus, WorkerStatus
from ai_company.services import cost_governor as cg
from ai_company.services.audit import record_audit
from ai_company.services.escalation import escalate_blocked_task
from ai_company.services.policy_engine import is_read_only_kind
from ai_company.services.progress import emit as progress
from ai_company.services.result_parser import failure_cause_of
from ai_company.services.tasks import create_task
from ai_company.services.validation import (
    finalize_judge,
    is_implementation_task,
    is_judge_task,
    is_validation_task,
    record_validation_result,
    start_validation_gate,
)
from ai_company.ui_ja import role_ja


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def failure_cause(execution: Execution | None, *, timed_out: bool = False) -> str:
    """Short machine cause of a failed run (see ``result_parser.failure_cause_of``)."""
    if execution is None:
        return "timeout" if timed_out else ""
    return failure_cause_of(
        execution.result_json, exit_code=execution.exit_code, timed_out=timed_out
    )


_GUIDANCE_CHARS = 1_500


def _reopen_read_only_parent(
    session: Session,
    *,
    parent: Task,
    replan: Task,
    worker: Worker,
    decision: str,
    target_role: str,
    guidance: str,
    config: AppConfig,
) -> None:
    from ai_company.services.policy_engine import evaluate_dispatch
    from ai_company.services.redact import redact_text

    role = parent.role_required or target_role
    if target_role != role:
        # REASSIGN only when the new role may run this read-only kind.
        if evaluate_dispatch(task=parent, role=target_role, config=config).allowed:
            role = target_role
    note = redact_text(guidance.strip())[:_GUIDANCE_CHARS]
    marker = "\n\n[上司の指示]\n"
    base = (parent.objective or "").split(marker, 1)[0]
    parent.objective = f"{base}{marker}{note}" if note else base
    parent.role_required = role
    parent.retry_count = 0
    parent.status = TaskStatus.READY
    session.flush()
    record_audit(
        session,
        event_type="task_reopened_by_supervisor",
        actor_type="worker",
        actor_id=worker.id,
        target_type="task",
        target_id=parent.id,
        metadata={
            "replan_id": replan.id,
            "supervisor_role": replan.role_required,
            "decision": decision,
            "executor_role": role,
            "kind": str(parent.kind),
        },
    )
    progress(
        f"上司対応完了  {role_ja(replan.role_required)}が方針決定 → "
        f"{role_ja(role)} へ差し戻し（読み取り専用のため修正タスクは作らない）  "
        f"タスク={parent.id}"
    )


def _is_closure(task: Task) -> bool:
    from ai_company.services.closure_loops import is_closure_task

    return is_closure_task(task)




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

    actual_cost = (
        execution.actual_cost
        if execution.actual_cost is not None
        else task.estimated_cost
    )
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

        from ai_company.services.closure_loops import apply_closure_result, is_closure_task

        if is_closure_task(task):
            payload = execution.result_json or {}
            apply_closure_result(
                session, task=task, payload=payload, config=cfg
            )
            record_audit(
                session,
                event_type="task_completed",
                actor_type="worker",
                actor_id=worker.id,
                target_type="task",
                target_id=task.id,
                metadata={
                    "execution_id": execution.id,
                    "kind": task.kind,
                    "closure_verdict": (payload.get("closure_verdict") or "CLEAR"),
                },
            )
            return execution

        if task.kind in {TaskKind.ATTENDANCE_REVIEW, "attendance_review"}:
            # 招集見直し: 秘書側で検証し、追加招集した部の部門作業／部長所見を起票（追加のみ）
            from ai_company.services.attendance_review import apply_attendance_review
            from ai_company.services.tasks import refresh_dependents

            apply_attendance_review(
                session, task=task, payload=execution.result_json or {}, config=cfg
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
                metadata={"execution_id": execution.id, "kind": "attendance_review"},
            )
            refresh_dependents(session, task.id)
            return execution

        if task.kind in {TaskKind.HEAD_BRIEF, "head_brief"}:
            # 部長所見: 秘書側で検証し、自部門の Work Order だけを起票（失敗はテンプレートへ）
            from ai_company.services.head_briefs import apply_head_brief
            from ai_company.services.tasks import refresh_dependents

            apply_head_brief(
                session, task=task, payload=execution.result_json or {}, config=cfg
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
                metadata={"execution_id": execution.id, "kind": "head_brief"},
            )
            refresh_dependents(session, task.id)
            return execution

        if task.kind in {TaskKind.DEPARTMENT_WORK, "department_work"}:
            # 会議の部門作業指示: 文書成果物。検証ゲートには入らず、自問ループで点検。
            # proposed_tasks は無視する（連鎖で起票が増える経路を作らない）。
            from ai_company.services.department_orders import (
                record_department_work_result,
                write_department_deliverable,
            )
            from ai_company.services.tasks import refresh_dependents

            record_department_work_result(task, execution.result_json or {})
            # 成果物ファイルは秘書側が書く（Worker は読み取り専用のまま）
            doc_path = write_department_deliverable(task, execution.result_json or {}, cfg)
            task.status = TaskStatus.DONE
            session.flush()
            meta: dict[str, Any] = {"execution_id": execution.id, "kind": "department_work"}
            if doc_path is not None:
                meta["document_path"] = json.loads(task.constraints or "{}").get(
                    "work_order_document_path"
                )
            record_audit(
                session,
                event_type="task_completed",
                actor_type="worker",
                actor_id=worker.id,
                target_type="task",
                target_id=task.id,
                metadata=meta,
            )
            refresh_dependents(session, task.id)
            return execution

        # Implementation / repair / manager replan
        if is_implementation_task(task):
            if task.kind in {"replan", TaskKind.REPLAN}:
                # A supervisor decision becomes another queued task. The supervisor
                # never starts a worker directly.
                task.status = TaskStatus.DONE
                session.flush()
                record_audit(
                    session,
                    event_type="task_completed",
                    actor_type="worker",
                    actor_id=worker.id,
                    target_type="task",
                    target_id=task.id,
                    metadata={"execution_id": execution.id, "kind": "replan"},
                )
                parent = (
                    session.get(Task, task.parent_task_id) if task.parent_task_id else None
                )
                if parent is not None:
                    payload = execution.result_json or {}
                    decision = str(payload.get("decision") or "RETRY").upper()
                    guidance = (
                        str(payload.get("instructions") or payload.get("summary") or "")
                        or str(payload.get("reason") or "")
                        or f"Manager replan after: {task.objective}"
                    )
                    if decision == "ESCALATE":
                        escalate_blocked_task(
                            session,
                            task=task,
                            reason=(
                                f"supervisor_requested:{task.role_required}:"
                                f"{payload.get('reason') or guidance}"
                            ),
                            config=cfg,
                        )
                        return execution

                    from ai_company.roles import role_def

                    requested_role = str(payload.get("target_role") or "").strip()
                    target_role = parent.role_required or "coding_worker"
                    if decision == "REASSIGN" and requested_role:
                        candidate = role_def(requested_role)
                        if candidate.layer == "worker":
                            target_role = requested_role
                    if is_read_only_kind(parent.kind):
                        # Read-only work (closure audits, department work, …) has
                        # nothing to "repair": a REPAIR task needs code.edit, which
                        # these roles lack, so it was denied every cycle (live Cursor
                        # 2026-11-18). Re-open the original task with the guidance.
                        _reopen_read_only_parent(
                            session,
                            parent=parent,
                            replan=task,
                            worker=worker,
                            decision=decision,
                            target_role=target_role,
                            guidance=guidance,
                            config=cfg,
                        )
                        return execution
                    parent.retry_count = 0
                    repair = create_task(
                        session,
                        title=f"Repair: {parent.title}",
                        priority=min(100, parent.priority + 5),
                        project_id=parent.project_id,
                        objective=(
                            f"Follow {task.role_required} guidance.\n{guidance}"
                        ),
                        role_required=target_role,
                        estimated_cost=parent.estimated_cost,
                        parent_task_id=parent.id,
                        kind=TaskKind.REPAIR,
                        risk_level=parent.risk_level,
                        config=cfg,
                    )
                    parent.status = TaskStatus.FAILED
                    session.flush()
                    record_audit(
                        session,
                        event_type="repair_task_created",
                        actor_type="worker",
                        actor_id=worker.id,
                        target_type="task",
                        target_id=repair.id,
                        metadata={
                            "parent": parent.id,
                            "via": "manager_replan",
                            "replan_id": task.id,
                            "handler_role": task.role_required,
                            "supervisor_role": task.role_required,
                            "decision": decision,
                            "executor_role": target_role,
                        },
                    )
                    progress(
                        f"上司対応完了  {role_ja(task.role_required)}が方針決定 → "
                        f"{role_ja(target_role)} へ修正指示  "
                        f"修正={repair.id}  親={parent.id}"
                    )
                return execution

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
            findings=[
                {
                    "severity": "high",
                    "category": "execution",
                    "description": "validation worker failed",
                }
            ],
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
    if task.retry_count > task.max_retries and task.kind in {TaskKind.HEAD_BRIEF, "head_brief"}:
        # 部長は reports_to 経路に入らない: 上申せず、保留テンプレートを起票して閉じる。
        from ai_company.services.head_briefs import close_failed_head_brief

        close_failed_head_brief(
            session,
            task=task,
            reason=f"retries_exhausted:{task.retry_count}/{task.max_retries}",
            config=cfg,
        )
        cg.record_failure_and_maybe_trip(session)
    elif task.retry_count > task.max_retries and task.kind in {
        TaskKind.ATTENDANCE_REVIEW,
        "attendance_review",
    }:
        # 招集見直しも上申しない: キーワード判定の出席者のまま閉じる。
        from ai_company.services.attendance_review import close_failed_attendance_review

        close_failed_attendance_review(
            session,
            task=task,
            reason=f"retries_exhausted:{task.retry_count}/{task.max_retries}",
            config=cfg,
        )
        cg.record_failure_and_maybe_trip(session)
    elif task.retry_count > task.max_retries:
        reason = f"retries_exhausted:{task.retry_count}/{task.max_retries}"
        cause = failure_cause(execution, timed_out=timed_out)
        if cause:
            reason = f"{reason} (last_error={cause})"
        escalate_blocked_task(
            session,
            task=task,
            reason=reason,
            config=cfg,
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
