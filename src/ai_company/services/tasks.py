"""Project and task services with queue readiness."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_company.config import AppConfig, load_config
from ai_company.db.models import Project, Task, TaskDependency
from ai_company.enums import ProjectStatus, TaskStatus
from ai_company.ids import new_id
from ai_company.services.audit import record_audit

TERMINAL_DONE = {TaskStatus.DONE}
BLOCKING_STATUSES = {
    TaskStatus.BACKLOG,
    TaskStatus.WAITING,
    TaskStatus.READY,
    TaskStatus.RUNNING,
    TaskStatus.REVIEW,
    TaskStatus.REVIEW_REQUIRED,
    TaskStatus.FAILED,
    TaskStatus.BLOCKED,
    TaskStatus.DEFERRED_BUDGET,
    TaskStatus.CANCELLED,
}


def create_project(
    session: Session,
    *,
    name: str,
    goal: str = "",
    budget_soft: float | None = None,
    budget_hard: float | None = None,
) -> Project:
    project = Project(
        id=new_id("PROJECT"),
        name=name,
        goal=goal,
        status=ProjectStatus.ACTIVE,
        budget_soft=budget_soft,
        budget_hard=budget_hard,
    )
    session.add(project)
    session.flush()
    record_audit(
        session,
        event_type="project_created",
        actor_type="human",
        actor_id="owner",
        target_type="project",
        target_id=project.id,
        metadata={"name": name},
    )
    return project


def get_or_create_default_project(session: Session) -> Project:
    existing = session.scalar(select(Project).order_by(Project.created_at.asc()).limit(1))
    if existing:
        return existing
    return create_project(session, name="default", goal="Default project")


def create_task(
    session: Session,
    *,
    title: str,
    priority: int = 50,
    project_id: str | None = None,
    objective: str = "",
    role_required: str = "coding_worker",
    max_retries: int | None = None,
    estimated_cost: float | None = None,
    depends_on: list[str] | None = None,
    parent_task_id: str | None = None,
    kind: str = "implementation",
    risk_level: str = "medium",
    validation_plan_id: str | None = None,
    review_type: str | None = None,
    source_execution_id: str | None = None,
    config: AppConfig | None = None,
) -> Task:
    cfg = config or load_config()
    project = (
        session.get(Project, project_id)
        if project_id
        else get_or_create_default_project(session)
    )
    if project is None:
        raise ValueError(f"Project not found: {project_id}")

    task = Task(
        id=new_id("TASK"),
        project_id=project.id,
        parent_task_id=parent_task_id,
        title=title,
        objective=objective or title,
        role_required=role_required,
        kind=kind,
        risk_level=risk_level,
        validation_plan_id=validation_plan_id,
        review_type=review_type,
        source_execution_id=source_execution_id,
        status=TaskStatus.BACKLOG,
        priority=priority,
        max_retries=max_retries if max_retries is not None else cfg.default_max_retries,
        estimated_cost=(
            estimated_cost if estimated_cost is not None else cfg.default_estimated_cost
        ),
    )
    session.add(task)
    session.flush()

    for dep_id in depends_on or []:
        if session.get(Task, dep_id) is None:
            raise ValueError(f"Dependency task not found: {dep_id}")
        session.add(TaskDependency(task_id=task.id, depends_on_task_id=dep_id))
    session.flush()

    record_audit(
        session,
        event_type="task_created",
        actor_type="human",
        actor_id="owner",
        target_type="task",
        target_id=task.id,
        metadata={"title": title, "priority": priority, "project_id": project.id},
    )

    refresh_task_readiness(session, task.id)
    return task


def dependencies_satisfied(session: Session, task_id: str) -> bool:
    deps = session.scalars(
        select(TaskDependency).where(TaskDependency.task_id == task_id)
    ).all()
    if not deps:
        return True
    for dep in deps:
        parent = session.get(Task, dep.depends_on_task_id)
        if parent is None or parent.status != TaskStatus.DONE:
            return False
    return True


def refresh_task_readiness(session: Session, task_id: str) -> Task:
    task = session.get(Task, task_id)
    if task is None:
        raise ValueError(f"Task not found: {task_id}")

    if task.status in {
        TaskStatus.RUNNING,
        TaskStatus.REVIEW,
        TaskStatus.DONE,
        TaskStatus.CANCELLED,
        TaskStatus.BLOCKED,
    }:
        return task

    # FAILED may be re-queued by retry logic elsewhere
    if task.status == TaskStatus.FAILED:
        return task

    if task.status == TaskStatus.DEFERRED_BUDGET:
        # keep deferred until scheduler reconsiders
        return task

    if dependencies_satisfied(session, task.id):
        if task.status != TaskStatus.READY:
            task.status = TaskStatus.READY
            session.flush()
            record_audit(
                session,
                event_type="task_ready",
                actor_type="system",
                actor_id="queue",
                target_type="task",
                target_id=task.id,
                metadata={"priority": task.priority},
            )
    else:
        if task.status != TaskStatus.WAITING:
            task.status = TaskStatus.WAITING
            session.flush()
            record_audit(
                session,
                event_type="task_waiting",
                actor_type="system",
                actor_id="queue",
                target_type="task",
                target_id=task.id,
                metadata={},
            )
    return task


def refresh_dependents(session: Session, completed_task_id: str) -> None:
    dependents = session.scalars(
        select(TaskDependency).where(TaskDependency.depends_on_task_id == completed_task_id)
    ).all()
    for dep in dependents:
        refresh_task_readiness(session, dep.task_id)


def list_tasks(
    session: Session,
    *,
    status: str | None = None,
    project_id: str | None = None,
) -> list[Task]:
    stmt = select(Task).order_by(Task.priority.desc(), Task.created_at.asc())
    if status:
        stmt = stmt.where(Task.status == status)
    if project_id:
        stmt = stmt.where(Task.project_id == project_id)
    return list(session.scalars(stmt).all())


def list_ready_tasks(session: Session, *, limit: int | None = None) -> list[Task]:
    stmt = (
        select(Task)
        .where(Task.status == TaskStatus.READY)
        .order_by(Task.priority.desc(), Task.created_at.asc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt).all())
