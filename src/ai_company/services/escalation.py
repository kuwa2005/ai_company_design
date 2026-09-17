"""Escalation: nearest AI supervisor first, human president last."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_company.config import AppConfig, load_config
from ai_company.db.models import Approval, Task
from ai_company.enums import ApprovalStatus, TaskKind, TaskStatus
from ai_company.ids import new_id
from ai_company.roles import escalation_route_text, format_role_label, supervisor_of
from ai_company.services.audit import record_audit
from ai_company.services.progress import emit as progress
from ai_company.services.staffing import ensure_staffing_request
from ai_company.services.tasks import create_task

MANAGER_ROLE = "engineering_manager"
HUMAN_ROLE = "human_owner"

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
    """Backward-compatible count of all supervisor decisions for a root task."""
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


def count_supervisor_replans(
    session: Session, parent_task_id: str, supervisor_role: str
) -> int:
    return len(
        list(
            session.scalars(
                select(Task).where(
                    Task.parent_task_id == parent_task_id,
                    Task.kind.in_((TaskKind.REPLAN, "replan")),
                    Task.role_required == supervisor_role,
                )
            ).all()
        )
    )


def _root_task(session: Session, task: Task) -> Task:
    """Follow supervisory/repair links to the original reported task."""
    current = task
    seen = {task.id}
    while current.parent_task_id and current.kind in {
        TaskKind.REPLAN,
        TaskKind.REPAIR,
        "replan",
        "repair",
    }:
        parent = session.get(Task, current.parent_task_id)
        if parent is None or parent.id in seen:
            break
        seen.add(parent.id)
        current = parent
    return current


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
    route = escalation_route_text(
        worker_role=task.role_required or "coding_worker", stage="human"
    )
    progress(
        f"エスカレーション  {route}  "
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


def _create_supervisor_replan(
    session: Session,
    *,
    root_task: Task,
    reported_task: Task,
    supervisor_role: str,
    reason: str,
    config: AppConfig,
) -> Task:
    """Create a decision task for the next supervisor; never spawn directly."""
    ensure_staffing_request(
        session,
        project_id=root_task.project_id,
        role=supervisor_role,
        requested_count=1,
        reason=f"supervise_failure:{root_task.id}:{reported_task.id}",
        auto_approve=True,
        config=config,
    )
    replan_n = count_supervisor_replans(session, root_task.id, supervisor_role) + 1
    replan = create_task(
        session,
        title=f"Supervisor decision [{supervisor_role}] #{replan_n}: {root_task.title}",
        priority=min(100, root_task.priority + 10),
        project_id=root_task.project_id,
        objective=(
            f"You are {supervisor_role}, the next supervisor for "
            f"{reported_task.role_required}. The reported work is 「{root_task.title}」 "
            f"({root_task.id}). Diagnose the failure and choose RETRY, REASSIGN, or "
            f"ESCALATE. Prefer a concrete instruction to a subordinate for technical "
            f"problems. Escalate only when this level lacks authority or information. "
            f"Failure report: {reason}"
        ),
        role_required=supervisor_role,
        estimated_cost=root_task.estimated_cost,
        parent_task_id=root_task.id,
        kind=TaskKind.REPLAN,
        risk_level=root_task.risk_level,
        max_retries=config.default_max_retries,
        config=config,
    )
    root_task.status = TaskStatus.FAILED
    if reported_task.id != root_task.id and reported_task.status != TaskStatus.DONE:
        reported_task.status = TaskStatus.FAILED
    session.flush()
    progress(
        f"上申  {format_role_label(reported_task.role_required)} → "
        f"{format_role_label(supervisor_role)}  "
        f"失敗タスク={root_task.id}「{root_task.title}」  "
        f"→ 判断タスク={replan.id}  "
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
            "parent": root_task.id,
            "reported_task": reported_task.id,
            "reason": reason,
            "replan_index": replan_n,
            "role": supervisor_role,
            "handler": supervisor_role,
            "from_role": reported_task.role_required,
            "route": f"{reported_task.role_required}→{supervisor_role}",
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
    Route failure through the reporting graph, one supervisor at a time.

    Human approval is created only for explicit governance boundaries or after
    every AI supervisor level has been tried.

    Pedantic (non-security) loops may auto-fire the stubborn supervisor and
    mark the root task DONE instead of escalating forever.
    """
    from ai_company.services.dismissal import maybe_auto_fire, reasons_are_fireable

    cfg = config or load_config()

    root = _root_task(session, task)

    # Stubborn chain exhausted / pedantic human escalate → fire, don't bother president
    if reason_requires_human(reason) and reasons_are_fireable(reason):
        fired = maybe_auto_fire(
            session,
            task=root,
            reason=reason,
            fired_role="stubborn_supervisor",
            config=cfg,
        )
        if fired is not None:
            return fired
        if getattr(cfg.repair_loop, "auto_fire_stubborn_supervisor", True):
            from ai_company.services.dismissal import fire_stubborn_supervisor

            return fire_stubborn_supervisor(
                session,
                task=root,
                reason=f"auto_fire_human_gate:{reason}",
                fired_role="stubborn_supervisor",
                config=cfg,
            )
        return _create_human_approval(session, task=root, reason=reason)

    if reason_requires_human(reason):
        return _create_human_approval(session, task=root, reason=reason)

    # Mid-flight: enough non-security rejects already → fire before another replan
    fired = maybe_auto_fire(
        session,
        task=root,
        reason=reason,
        fired_role=task.role_required,
        config=cfg,
    )
    if fired is not None:
        return fired

    current_role = task.role_required or "coding_worker"
    supervisor_role = supervisor_of(current_role)
    configured_max = getattr(cfg.repair_loop, "manager_max_replans", 2)
    max_replans = 2 if configured_max is None else max(0, int(configured_max))

    # Exhausting one level promotes the issue to that supervisor's supervisor.
    while supervisor_role not in {None, HUMAN_ROLE} and count_supervisor_replans(
        session, root.id, supervisor_role
    ) >= max_replans:
        current_role = supervisor_role
        supervisor_role = supervisor_of(current_role)

    if supervisor_role in {None, HUMAN_ROLE}:
        human_reason = (
            f"manager_exhausted:supervisor_chain:after={current_role}:{reason}"
        )
        if reasons_are_fireable(human_reason) and getattr(
            cfg.repair_loop, "auto_fire_stubborn_supervisor", True
        ):
            from ai_company.services.dismissal import fire_stubborn_supervisor

            return fire_stubborn_supervisor(
                session,
                task=root,
                reason=f"auto_fire:{human_reason}",
                fired_role=current_role,
                config=cfg,
            )
        return _create_human_approval(
            session,
            task=root,
            reason=human_reason,
        )

    return _create_supervisor_replan(
        session,
        root_task=root,
        reported_task=task,
        supervisor_role=supervisor_role,
        reason=reason,
        config=cfg,
    )


def pending_approval_task_ids(session: Session) -> set[str]:
    from ai_company.services.secretary import _task_id_from_reason

    ids: set[str] = set()
    for a in session.scalars(
        select(Approval).where(Approval.status == ApprovalStatus.PENDING)
    ).all():
        tid = _task_id_from_reason(a.reason or "")
        if tid:
            ids.add(tid)
    return ids


def list_orphaned_blocked(
    session: Session, *, project_id: str | None = None
) -> list[Task]:
    """BLOCKED tasks with no PENDING human approval (stuck after reject/reconsider)."""
    pending_ids = pending_approval_task_ids(session)
    stmt = select(Task).where(Task.status == TaskStatus.BLOCKED)
    if project_id:
        stmt = stmt.where(Task.project_id == project_id)
    return [t for t in session.scalars(stmt).all() if t.id not in pending_ids]


def hand_to_manager(
    session: Session,
    *,
    task: Task,
    reason: str = "orphan_recover:hand_to_nearest_manager",
    config: AppConfig | None = None,
) -> Approval | Task:
    """Explicitly give a stuck task to engineering_manager (not the president)."""
    cfg = config or load_config()
    return escalate_blocked_task(session, task=task, reason=reason, config=cfg)


def recover_orphaned_blocked_to_manager(
    session: Session,
    *,
    project_id: str | None = None,
    config: AppConfig | None = None,
) -> list[str]:
    """
    Auto-recover BLOCKED tasks that have no PENDING approval.

    Typical case: human chose 「再度検討せよ」/却下, or legacy human-escalation
    left the task blocked with an empty approval queue. Nearest manager takes over.
    """
    cfg = config or load_config()
    recovered: list[str] = []
    for task in list_orphaned_blocked(session, project_id=project_id):
        # Prefer last known failure reason from rejected approval when present
        last = session.scalar(
            select(Approval)
            .where(Approval.reason.contains(task.id))
            .order_by(Approval.created_at.desc())
            .limit(1)
        )
        base = "orphan_recover:blocked_without_pending"
        if last and last.reason:
            # strip resolution suffix
            base_reason = last.reason.split(" | resolution:", 1)[0].strip()
            reason = f"orphan_recover:{base_reason}"
        else:
            reason = base
        hand_to_manager(session, task=task, reason=reason, config=cfg)
        recovered.append(task.id)
        progress(
            f"孤児停止を回収  {task.id}「{task.title}」→ "
            f"{format_role_label(MANAGER_ROLE)} へ移管（社長キュー空のため）"
        )
    return recovered


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
