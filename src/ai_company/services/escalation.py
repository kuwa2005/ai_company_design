"""Human escalation for BLOCKED / policy decisions."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_company.db.models import Approval, Task
from ai_company.enums import ApprovalStatus, TaskStatus
from ai_company.ids import new_id
from ai_company.services.audit import record_audit


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def escalate_blocked_task(
    session: Session,
    *,
    task: Task,
    reason: str,
) -> Approval:
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
    record_audit(
        session,
        event_type="approval_requested",
        actor_type="system",
        actor_id="escalation",
        target_type="approval",
        target_id=approval.id,
        metadata={"task_id": task.id, "reason": reason},
    )
    record_audit(
        session,
        event_type="task_blocked",
        actor_type="system",
        actor_id="escalation",
        target_type="task",
        target_id=task.id,
        metadata={"approval_id": approval.id, "reason": reason},
    )
    return approval


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

    # If approved unblock: requeue related task when reason embeds task=
    if approve and "task=" in approval.reason:
        # parse task id between task= and ;
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
