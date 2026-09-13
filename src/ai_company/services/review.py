"""Review task enqueue and resolution (PASS → DONE, REJECT → retry/BLOCKED)."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_company.config import AppConfig, load_config
from ai_company.db.models import Task
from ai_company.enums import TaskStatus
from ai_company.services.audit import record_audit
from ai_company.services.escalation import escalate_blocked_task
from ai_company.services.tasks import create_task, refresh_dependents

REVIEW_ROLES = {"reviewer", "qa", "qa_worker"}


def role_requires_review(role: str) -> bool:
    return role not in REVIEW_ROLES


def enqueue_review_task(
    session: Session,
    *,
    parent: Task,
    config: AppConfig | None = None,
) -> Task:
    cfg = config or load_config()
    review_role = cfg.settings.review.role
    review = create_task(
        session,
        title=f"Review: {parent.title}",
        priority=min(100, parent.priority + 10),
        project_id=parent.project_id,
        objective=(
            f"Review parent task {parent.id} against acceptance criteria. "
            f"Respond with status completed (PASS) or failed (REJECT)."
        ),
        role_required=review_role,
        estimated_cost=parent.estimated_cost * 0.5,
        parent_task_id=parent.id,
        config=cfg,
    )
    parent.status = TaskStatus.REVIEW
    session.flush()
    record_audit(
        session,
        event_type="task_review_enqueued",
        actor_type="system",
        actor_id="review",
        target_type="task",
        target_id=parent.id,
        metadata={"review_task_id": review.id, "role": review_role},
    )
    return review


def complete_parent_after_review_pass(session: Session, *, review_task: Task) -> None:
    if not review_task.parent_task_id:
        return
    parent = session.get(Task, review_task.parent_task_id)
    if parent is None:
        return
    parent.status = TaskStatus.DONE
    session.flush()
    record_audit(
        session,
        event_type="task_completed",
        actor_type="system",
        actor_id="review",
        target_type="task",
        target_id=parent.id,
        metadata={"via_review_task": review_task.id},
    )
    refresh_dependents(session, parent.id)


def handle_review_reject(session: Session, *, review_task: Task) -> None:
    if not review_task.parent_task_id:
        return
    parent = session.get(Task, review_task.parent_task_id)
    if parent is None:
        return
    parent.retry_count += 1
    record_audit(
        session,
        event_type="task_review_rejected",
        actor_type="system",
        actor_id="review",
        target_type="task",
        target_id=parent.id,
        metadata={
            "review_task_id": review_task.id,
            "retry_count": parent.retry_count,
        },
    )
    if parent.retry_count > parent.max_retries:
        parent.status = TaskStatus.BLOCKED
        session.flush()
        escalate_blocked_task(
            session,
            task=parent,
            reason=f"review_rejected_retries_exhausted:{parent.retry_count}",
        )
    else:
        parent.status = TaskStatus.READY
        session.flush()
        record_audit(
            session,
            event_type="task_ready",
            actor_type="system",
            actor_id="review_retry",
            target_type="task",
            target_id=parent.id,
            metadata={"after_review_reject": True},
        )


def find_open_review_for_parent(session: Session, parent_id: str) -> Task | None:
    return session.scalar(
        select(Task)
        .where(
            Task.parent_task_id == parent_id,
            Task.role_required.in_(list(REVIEW_ROLES)),
            Task.status.in_(
                [TaskStatus.READY, TaskStatus.WAITING, TaskStatus.RUNNING, TaskStatus.REVIEW]
            ),
        )
        .limit(1)
    )
