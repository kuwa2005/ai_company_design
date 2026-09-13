"""Lease reclaim and zombie worker cleanup (fail closed, no duplicate runs)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_company.config import AppConfig, load_config
from ai_company.db.models import Task, Worker
from ai_company.enums import TaskStatus, WorkerStatus
from ai_company.services.audit import record_audit
from ai_company.services.workers import terminate_worker

ACTIVE_WORKER = {
    WorkerStatus.HIRED,
    WorkerStatus.STARTING,
    WorkerStatus.ACTIVE,
    WorkerStatus.REPORTING,
}


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


@dataclass
class CleanupResult:
    leases_reclaimed: int = 0
    zombies_killed: int = 0
    details: list[str] | None = None

    def __post_init__(self) -> None:
        if self.details is None:
            self.details = []


def reclaim_expired_leases(session: Session) -> int:
    now = _utcnow()
    tasks = list(
        session.scalars(
            select(Task).where(
                Task.leased_by.is_not(None),
                Task.lease_expires_at.is_not(None),
                Task.lease_expires_at <= now,
            )
        ).all()
    )
    count = 0
    for task in tasks:
        # Only reclaim if not actively running with a live worker
        if task.status == TaskStatus.RUNNING:
            live = session.scalar(
                select(Worker).where(
                    Worker.task_id == task.id,
                    Worker.status.in_(list(ACTIVE_WORKER)),
                )
            )
            if live is not None:
                continue
            # Stuck RUNNING without live worker → READY for retry scheduling
            task.status = TaskStatus.READY
        task.leased_by = None
        task.lease_expires_at = None
        count += 1
        record_audit(
            session,
            event_type="lease_reclaimed",
            actor_type="system",
            actor_id="cleanup",
            target_type="task",
            target_id=task.id,
            metadata={"status": task.status},
        )
    session.flush()
    return count


def cleanup_zombie_workers(
    session: Session, *, config: AppConfig | None = None
) -> int:
    cfg = config or load_config()
    timeout = timedelta(minutes=cfg.settings.worker.hard_timeout_minutes)
    now = _utcnow()
    zombies = list(
        session.scalars(
            select(Worker).where(Worker.status.in_(list(ACTIVE_WORKER)))
        ).all()
    )
    killed = 0
    for worker in zombies:
        hired = worker.hired_at or now
        # SQLite may return naive datetime
        if hired.tzinfo is not None:
            hired = hired.replace(tzinfo=None)
        if now - hired < timeout:
            continue
        terminate_worker(session, worker=worker, status=WorkerStatus.KILLED)
        task = session.get(Task, worker.task_id)
        if task is not None and task.status == TaskStatus.RUNNING:
            task.status = TaskStatus.READY
            task.leased_by = None
            task.lease_expires_at = None
            record_audit(
                session,
                event_type="zombie_requeued",
                actor_type="system",
                actor_id="cleanup",
                target_type="task",
                target_id=task.id,
                metadata={"worker_id": worker.id},
            )
        killed += 1
        record_audit(
            session,
            event_type="zombie_killed",
            actor_type="system",
            actor_id="cleanup",
            target_type="worker",
            target_id=worker.id,
            metadata={},
        )
    session.flush()
    return killed


def run_cleanup(session: Session, *, config: AppConfig | None = None) -> CleanupResult:
    cfg = config or load_config()
    result = CleanupResult()
    result.leases_reclaimed = reclaim_expired_leases(session)
    result.zombies_killed = cleanup_zombie_workers(session, config=cfg)
    result.details = [
        f"leases_reclaimed={result.leases_reclaimed}",
        f"zombies_killed={result.zombies_killed}",
    ]
    return result
