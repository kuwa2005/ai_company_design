"""Product-level autonomous / no-look run loop (M10)."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ai_company.config import AppConfig, load_config
from ai_company.db.models import Artifact, Project, Task
from ai_company.enums import ProjectStatus, TaskKind, TaskStatus
from ai_company.ids import new_id
from ai_company.paths import executions_dir
from ai_company.services.audit import record_audit
from ai_company.services.progress import emit as progress
from ai_company.services.scheduler import SchedulerResult, run_once
from ai_company.services.secretary import exception_digest, human_summary
from ai_company.services.staffing import auto_approve_open_requests
from ai_company.services.workers import RunnerKind


TERMINAL_TASK = {
    TaskStatus.DONE,
    TaskStatus.BLOCKED,
    TaskStatus.CANCELLED,
}


@dataclass
class AutonomousResult:
    cycles: int = 0
    started_total: int = 0
    project_status: str = ""
    stopped_reason: str = ""
    release_candidate_id: str | None = None
    summaries: list[str] = field(default_factory=list)
    last_scheduler: SchedulerResult | None = None


def _implementation_tasks(session: Session, project_id: str) -> list[Task]:
    return list(
        session.scalars(
            select(Task).where(
                Task.project_id == project_id,
                Task.kind.in_((TaskKind.IMPLEMENTATION, "implementation")),
            )
        ).all()
    )


def _project_complete(session: Session, project_id: str) -> bool:
    tasks = _implementation_tasks(session, project_id)
    if not tasks:
        return False
    return all(t.status == TaskStatus.DONE for t in tasks)


def _has_blocking_exception(session: Session, project_id: str) -> bool:
    blocked = int(
        session.scalar(
            select(func.count())
            .select_from(Task)
            .where(Task.project_id == project_id, Task.status == TaskStatus.BLOCKED)
        )
        or 0
    )
    return blocked > 0


def _idle(session: Session, project_id: str) -> bool:
    """No READY/RUNNING work left for the project (and dependents)."""
    active = int(
        session.scalar(
            select(func.count())
            .select_from(Task)
            .where(
                Task.project_id == project_id,
                Task.status.in_(
                    (
                        TaskStatus.READY,
                        TaskStatus.RUNNING,
                        TaskStatus.WAITING,
                        TaskStatus.REVIEW_REQUIRED,
                        TaskStatus.FAILED,
                    )
                ),
            )
        )
        or 0
    )
    return active == 0


def create_release_candidate(
    session: Session, *, project: Project
) -> Artifact:
    out_dir = executions_dir() / "release_candidates"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{project.id}.md"
    summary = human_summary(session, project_id=project.id)
    digest = exception_digest(session, project_id=project.id)
    body = (
        f"# Release Candidate\n\n"
        f"Project: {project.id} — {project.name}\n\n"
        f"Goal: {project.goal}\n\n"
        f"## Summary\n\n{summary}\n\n"
        f"## Exceptions\n\n{digest}\n"
    )
    path.write_text(body, encoding="utf-8")
    art = Artifact(
        id=new_id("ART"),
        project_id=project.id,
        task_id=None,
        kind="release_candidate",
        path=str(path),
        metadata_json={"goal": project.goal},
    )
    session.add(art)
    session.flush()
    record_audit(
        session,
        event_type="release_candidate_created",
        actor_type="system",
        actor_id="autonomous",
        target_type="artifact",
        target_id=art.id,
        metadata={"project_id": project.id, "path": str(path)},
    )
    return art


def _progress_snapshot(session: Session, project_id: str) -> str:
    counts = {s: 0 for s in (
        TaskStatus.DONE,
        TaskStatus.READY,
        TaskStatus.WAITING,
        TaskStatus.RUNNING,
        TaskStatus.REVIEW_REQUIRED,
        TaskStatus.BLOCKED,
        TaskStatus.FAILED,
    )}
    rows = session.execute(
        select(Task.status, func.count())
        .where(Task.project_id == project_id)
        .group_by(Task.status)
    ).all()
    for status, n in rows:
        counts[str(status)] = int(n)
    return (
        f"DONE={counts.get(TaskStatus.DONE, 0)} "
        f"READY={counts.get(TaskStatus.READY, 0)} "
        f"WAITING={counts.get(TaskStatus.WAITING, 0)} "
        f"RUNNING={counts.get(TaskStatus.RUNNING, 0)} "
        f"REVIEW={counts.get(TaskStatus.REVIEW_REQUIRED, 0)} "
        f"BLOCKED={counts.get(TaskStatus.BLOCKED, 0)} "
        f"FAILED={counts.get(TaskStatus.FAILED, 0)}"
    )


def run_autonomous(
    session: Session,
    *,
    project_id: str,
    max_cycles: int = 40,
    runner: RunnerKind = "mock",
    mock_succeed: bool = True,
    config: AppConfig | None = None,
) -> AutonomousResult:
    """
    Drive Scheduler until implementation tasks are DONE, blocked, or idle.

    Humans are not required to open each Task; they get summaries + exceptions.
    """
    cfg = config or load_config()
    project = session.get(Project, project_id)
    if project is None:
        raise ValueError(f"Project not found: {project_id}")

    result = AutonomousResult(project_status=project.status)
    auto_approve_open_requests(session, project_id=project_id, config=cfg)
    progress(
        f"AUTONOMOUS begin  project={project_id}  runner={runner}  "
        f"max_cycles={max_cycles}  {_progress_snapshot(session, project_id)}"
    )

    for cycle in range(1, max_cycles + 1):
        result.cycles = cycle
        if _project_complete(session, project_id):
            project.status = ProjectStatus.DONE
            session.flush()
            art = create_release_candidate(session, project=project)
            result.release_candidate_id = art.id
            result.project_status = project.status
            result.stopped_reason = "all_implementation_done"
            result.summaries.append(human_summary(session, project_id=project_id))
            progress(f"AUTONOMOUS complete  reason=all_implementation_done  rc={art.id}")
            break

        if _has_blocking_exception(session, project_id):
            result.project_status = project.status
            result.stopped_reason = "blocked_exception"
            result.summaries.append(exception_digest(session, project_id=project_id))
            progress("AUTONOMOUS stop  reason=blocked_exception")
            break

        progress(
            f"CYCLE {cycle}/{max_cycles}  {_progress_snapshot(session, project_id)}"
        )
        sched = run_once(
            session,
            config=cfg,
            mock_succeed=mock_succeed,
            runner=runner,
            max_to_start=cfg.settings.scheduler.max_concurrent_workers,
        )
        result.last_scheduler = sched
        result.started_total += sched.started
        progress(
            f"CYCLE {cycle} summary  started={sched.started} denied={sched.denied} "
            f"deferred={sched.deferred} throttled={sched.throttled}  "
            f"{_progress_snapshot(session, project_id)}"
        )

        if sched.started == 0 and _idle(session, project_id):
            if _project_complete(session, project_id):
                project.status = ProjectStatus.DONE
                session.flush()
                art = create_release_candidate(session, project=project)
                result.release_candidate_id = art.id
                result.stopped_reason = "all_implementation_done"
                progress(
                    f"AUTONOMOUS complete  reason=all_implementation_done  rc={art.id}"
                )
            else:
                result.stopped_reason = "idle_incomplete"
                progress("AUTONOMOUS stop  reason=idle_incomplete")
            result.project_status = project.status
            result.summaries.append(human_summary(session, project_id=project_id))
            break

        if cycle == max_cycles:
            result.stopped_reason = "max_cycles"
            result.project_status = project.status
            result.summaries.append(exception_digest(session, project_id=project_id))
            progress("AUTONOMOUS stop  reason=max_cycles")
    else:
        result.stopped_reason = result.stopped_reason or "max_cycles"

    record_audit(
        session,
        event_type="autonomous_run_finished",
        actor_type="system",
        actor_id="autonomous",
        target_type="project",
        target_id=project_id,
        metadata={
            "cycles": result.cycles,
            "started_total": result.started_total,
            "stopped_reason": result.stopped_reason,
            "release_candidate_id": result.release_candidate_id,
        },
    )
    return result
