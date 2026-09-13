"""Escalation: nearest AI supervisor first, human president last."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_company.config import AppConfig, load_config
from ai_company.db.models import Approval, Task
from ai_company.enums import ApprovalStatus, TaskKind, TaskStatus
from ai_company.ids import new_id
from ai_company.roles import format_role_label, escalation_route_text
from ai_company.services.audit import record_audit
from ai_company.services.progress import emit as progress
from ai_company.services.staffing import ensure_staffing_request
from ai_company.services.tasks import create_task

MANAGER_ROLE = "engineering_manager"

# Reasons that skip AI manager and go straight to human owner.
_HUMAN_DIRECT_PREFIXES = (
    "policy:",
    "budget:",
    "manager_exhausted:",
    "usage_exhausted",
    "circuit",
    "monthly_hard",
    "project_hard",
)


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def count_manager_replans(session: Session, parent_task_id: str) -> int:
    return len(
        list(
            session.scalars(
                select(Task).where(
                    Task.parent_task_id == parent_task_id,
                    Task.kind.in_((TaskKind.REPLAN, "replan")),
                )
            ).all()
        )
    )


def reason_requires_human(reason: str) -> bool:
    r = (reason or "").lower()
    return any(r.startswith(p) or f";{p}" in f";{r}" for p in _HUMAN_DIRECT_PREFIXES)


def _create_human_approval(
    session: Session,
    *,
    task: Task,
    reason: str,
) -> Approval:
    task.status = TaskStatus.BLOCKED
    session.flush()
    approval = Approval(
        id=new_id("APPR"),
        action="task.unblock_or_replan",
        requested_by="system",
        approved_by=None,
        status=ApprovalStatus.PENDING,
        reason=f"{reason}; task={task.id}; title={task.title}",
    )
    session.add(approval)
    session.flush()
    progress(
        f"エスカレーション  {escalation_route_text(worker_role=task.role_required or 'coding_worker', stage='human')}  "
        f"タスク={task.id}「{task.title}」  承認={approval.id}"
    )
    record_audit(
        session,
        event_type="approval_requested",
        actor_type="system",
        actor_id="escalation",
        target_type="approval",
        target_id=approval.id,
        metadata={
            "task_id": task.id,
            "reason": reason,
            "target": "human",
            "target_role": "human_owner",
            "from_role": task.role_required,
        },
    )
    record_audit(
        session,
        event_type="task_blocked",
        actor_type="system",
        actor_id="escalation",
        target_type="task",
        target_id=task.id,
        metadata={"approval_id": approval.id, "reason": reason, "handler": "human_owner"},
    )
    return approval


def _create_manager_replan(
    session: Session,
    *,
    task: Task,
    reason: str,
    config: AppConfig,
) -> Task:
    """Nearest supervisor (engineering_manager) owns the failure — not the human."""
    ensure_staffing_request(
        session,
        project_id=task.project_id,
        role=MANAGER_ROLE,
        requested_count=1,
        reason=f"supervise_failure:{task.id}",
        auto_approve=True,
        config=config,
    )
    replan_n = count_manager_replans(session, task.id) + 1
    replan = create_task(
        session,
        title=f"Manager replan #{replan_n}: {task.title}",
        priority=min(100, task.priority + 10),
        project_id=task.project_id,
        objective=(
            f"You are the immediate supervisor for the worker who owns 「{task.title}」. "
            f"The worker/repair loop failed. Do NOT ask the human president. "
            f"Diagnose, decide a concrete fix plan, and emit coding guidance. "
            f"Failure reason: {reason}"
        ),
        role_required=MANAGER_ROLE,
        estimated_cost=task.estimated_cost,
        parent_task_id=task.id,
        kind=TaskKind.REPLAN,
        risk_level=task.risk_level,
        max_retries=1,
        config=config,
    )
    task.status = TaskStatus.FAILED
    session.flush()
    progress(
        f"エスカレーション  {escalation_route_text(worker_role=task.role_required or 'coding_worker', stage='manager_replan')}  "
        f"失敗タスク={task.id}「{task.title}」  "
        f"→ 上司タスク={replan.id}（{format_role_label(MANAGER_ROLE)}）  "
        f"理由={reason[:120]}"
    )
    record_audit(
        session,
        event_type="manager_replan_created",
        actor_type="system",
        actor_id="escalation",
        target_type="task",
        target_id=replan.id,
        metadata={
            "parent": task.id,
            "reason": reason,
            "replan_index": replan_n,
            "role": MANAGER_ROLE,
            "handler": MANAGER_ROLE,
            "from_role": task.role_required,
            "route": "worker→engineering_manager",
        },
    )
    return replan


def escalate_blocked_task(
    session: Session,
    *,
    task: Task,
    reason: str,
    config: AppConfig | None = None,
) -> Approval | Task:
    """
    Route failure to the nearest AI supervisor first.

    Human (president) only when:
    - reason is policy/budget/manager_exhausted, or
    - manager replan budget is exhausted.
    """
    cfg = config or load_config()

    # If the failing task is itself a manager replan, escalate the root parent to human.
    root = task
    if task.kind in {TaskKind.REPLAN, "replan"} and task.parent_task_id:
        parent = session.get(Task, task.parent_task_id)
        if parent is not None:
            root = parent
        return _create_human_approval(
            session,
            task=root,
            reason=f"manager_exhausted:replan_failed:{reason}",
        )

    if reason_requires_human(reason):
        return _create_human_approval(session, task=task, reason=reason)

    max_replans = getattr(cfg.repair_loop, "manager_max_replans", 2)
    if max_replans is None:
        max_replans = 2
    max_replans = int(max_replans)
    if count_manager_replans(session, task.id) >= max_replans:
        return _create_human_approval(
            session,
            task=task,
            reason=f"manager_exhausted:max_replans:{max_replans}:{reason}",
        )

    return _create_manager_replan(session, task=task, reason=reason, config=cfg)


def list_approvals(
    session: Session, *, status: str | None = None, limit: int = 50
) -> list[Approval]:
    stmt = select(Approval).order_by(Approval.created_at.desc()).limit(limit)
    if status:
        stmt = stmt.where(Approval.status == status)
    return list(session.scalars(stmt).all())


def resolve_approval(
    session: Session,
    *,
    approval_id: str,
    approve: bool,
    resolved_by: str = "owner",
    note: str = "",
) -> Approval:
    approval = session.get(Approval, approval_id)
    if approval is None:
        raise ValueError(f"Approval not found: {approval_id}")
    if approval.status != ApprovalStatus.PENDING:
        raise ValueError(f"Approval not pending: {approval.status}")

    approval.status = ApprovalStatus.APPROVED if approve else ApprovalStatus.REJECTED
    approval.approved_by = resolved_by
    approval.resolved_at = _utcnow()
    if note:
        approval.reason = f"{approval.reason} | resolution: {note}"
    session.flush()

    event = "approval_approved" if approve else "approval_rejected"
    record_audit(
        session,
        event_type=event,
        actor_type="human",
        actor_id=resolved_by,
        target_type="approval",
        target_id=approval.id,
        metadata={"note": note},
    )

    if approve and "task=" in approval.reason:
        part = approval.reason.split("task=", 1)[1]
        task_id = part.split(";", 1)[0].strip()
        task = session.get(Task, task_id)
        if task is not None:
            task.retry_count = 0
            task.status = TaskStatus.READY
            session.flush()
            record_audit(
                session,
                event_type="task_ready",
                actor_type="human",
                actor_id=resolved_by,
                target_type="task",
                target_id=task.id,
                metadata={"via_approval": approval.id},
            )
    return approval
