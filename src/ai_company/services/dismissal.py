"""Fire stubborn / pedantic supervisors mid-flight and ship the work."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_company.config import AppConfig, load_config
from ai_company.db.models import Approval, JudgeDecision, Task
from ai_company.enums import ApprovalStatus, TaskKind, TaskStatus
from ai_company.roles import format_role_label
from ai_company.services.audit import record_audit
from ai_company.services.progress import emit as progress
from ai_company.services.tasks import refresh_dependents

# Hard blockers: never auto-fire these away.
_HARD_MARKERS = (
    "security_fail",
    "critical_finding",
    "missing_required_reviews",
    "policy:",
    "budget:",
    "usage_exhausted",
    "circuit",
    "monthly_hard",
    "project_hard",
)

_OPEN_STATUSES = {
    TaskStatus.BACKLOG,
    TaskStatus.WAITING,
    TaskStatus.READY,
    TaskStatus.RUNNING,
    TaskStatus.REVIEW_REQUIRED,
    TaskStatus.FAILED,
    TaskStatus.BLOCKED,
    TaskStatus.DEFERRED_BUDGET,
    "BACKLOG",
    "WAITING",
    "READY",
    "RUNNING",
    "REVIEW_REQUIRED",
    "FAILED",
    "BLOCKED",
    "DEFERRED_BUDGET",
}


def reasons_are_fireable(reason: str | list[str] | None) -> bool:
    """True when failure is pedantic / non-security (safe to fire and ship)."""
    if reason is None:
        return False
    text = ",".join(reason) if isinstance(reason, list) else str(reason)
    if not text.strip():
        return False
    lower = text.lower()
    return not any(m in lower for m in _HARD_MARKERS)


def count_stubborn_rejects(session: Session, parent_task_id: str) -> int:
    """Count prior judge REJECT decisions that were fireable (non-hard)."""
    n = 0
    for d in session.scalars(
        select(JudgeDecision).where(JudgeDecision.task_id == parent_task_id)
    ).all():
        if str(d.verdict).upper() != "REJECT":
            continue
        if reasons_are_fireable(d.reasons):
            n += 1
    return n


def _root_implementation_task(session: Session, task: Task) -> Task:
    current = task
    seen = {task.id}
    while current.parent_task_id and current.kind in {
        TaskKind.REPLAN,
        TaskKind.REPAIR,
        TaskKind.VALIDATION,
        TaskKind.JUDGE,
        "replan",
        "repair",
        "validation",
        "judge",
    }:
        parent = session.get(Task, current.parent_task_id)
        if parent is None or parent.id in seen:
            break
        seen.add(parent.id)
        current = parent
    return current


def fire_stubborn_supervisor(
    session: Session,
    *,
    task: Task,
    reason: str,
    fired_role: str | None = None,
    actor_id: str = "system:dismissal",
    config: AppConfig | None = None,
) -> Task:
    """
    Dismiss pedantic supervisors mid-process: cancel open loops, mark DONE, unlock deps.

    Security / critical / policy failures must not use this path.
    """
    _ = config or load_config()
    root = _root_implementation_task(session, task)
    role_label = format_role_label(fired_role or "engineering_manager")

    cancelled: list[str] = []
    for child in session.scalars(
        select(Task).where(Task.parent_task_id == root.id)
    ).all():
        if child.status in _OPEN_STATUSES:
            child.status = TaskStatus.CANCELLED
            cancelled.append(child.id)

    # Close any pending human approvals tied to this task
    from ai_company.services.secretary import _task_id_from_reason

    closed_approvals: list[str] = []
    for appr in session.scalars(
        select(Approval).where(Approval.status == ApprovalStatus.PENDING)
    ).all():
        tid = _task_id_from_reason(appr.reason or "")
        if tid == root.id:
            appr.status = ApprovalStatus.REJECTED
            appr.approved_by = actor_id
            closed_approvals.append(appr.id)

    root.status = TaskStatus.DONE
    session.flush()
    refresh_dependents(session, root.id)

    progress(
        f"解雇  {role_label} を途中解雇 → 出荷優先で完了  "
        f"タスク={root.id}「{root.title}」  理由={reason[:140]}"
    )
    record_audit(
        session,
        event_type="supervisor_fired",
        actor_type="system" if actor_id.startswith("system") else "human",
        actor_id=actor_id,
        target_type="task",
        target_id=root.id,
        metadata={
            "reason": reason,
            "fired_role": fired_role or "stubborn_supervisor",
            "cancelled_children": cancelled,
            "closed_approvals": closed_approvals,
            "via": "dismissal",
        },
    )
    record_audit(
        session,
        event_type="task_completed",
        actor_type="system",
        actor_id="dismissal",
        target_type="task",
        target_id=root.id,
        metadata={"via": "supervisor_fired", "reason": reason},
    )
    return root


def maybe_auto_fire(
    session: Session,
    *,
    task: Task,
    reason: str,
    fired_role: str | None = None,
    config: AppConfig | None = None,
) -> Task | None:
    """If config allows and reason is pedantic, fire and return root; else None."""
    cfg = config or load_config()
    if not getattr(cfg.repair_loop, "auto_fire_stubborn_supervisor", True):
        return None
    if not reasons_are_fireable(reason):
        return None
    root = _root_implementation_task(session, task)
    threshold = int(getattr(cfg.repair_loop, "fire_after_non_security_rejects", 2) or 2)
    stubborn_n = count_stubborn_rejects(session, root.id)
    # Already at/over threshold, or reason says managers exhausted pedantry
    lower = reason.lower()
    exhausted = "manager_exhausted" in lower or "repair_loop_max_cycles" in lower
    if stubborn_n >= threshold or exhausted:
        return fire_stubborn_supervisor(
            session,
            task=root,
            reason=f"auto_fire:{reason}",
            fired_role=fired_role or "stubborn_supervisor",
            config=cfg,
        )
    return None
