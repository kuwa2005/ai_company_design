"""Scheduler: lease READY tasks, consult Cost Governor, hire mock workers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from ai_company.config import AppConfig, load_config
from ai_company.db.models import Task
from ai_company.enums import CostDecision
from ai_company.services import cost_governor as cg
from ai_company.services.audit import record_audit
from ai_company.services.tasks import list_ready_tasks
from ai_company.services.workers import RunnerKind, dispatch_worker, hire_worker


@dataclass
class SchedulerResult:
    attempted: int = 0
    started: int = 0
    denied: int = 0
    deferred: int = 0
    throttled: int = 0
    details: list[dict[str, str]] | None = None

    def __post_init__(self) -> None:
        if self.details is None:
            self.details = []


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def acquire_lease(session: Session, task: Task, *, holder: str, minutes: int = 10) -> bool:
    now = _utcnow()
    if task.leased_by and task.lease_expires_at and task.lease_expires_at > now:
        if task.leased_by != holder:
            return False
    task.leased_by = holder
    task.lease_expires_at = now + timedelta(minutes=minutes)
    session.flush()
    return True


def release_lease(session: Session, task: Task) -> None:
    task.leased_by = None
    task.lease_expires_at = None
    session.flush()


def run_once(
    session: Session,
    *,
    config: AppConfig | None = None,
    mock_succeed: bool = True,
    max_to_start: int | None = None,
    runner: RunnerKind = "mock",
    timeout_seconds: float | None = None,
) -> SchedulerResult:
    """
    One scheduling cycle:
    READY -> Cost check -> reserve -> hire -> runner -> DONE/TERMINATED
    """
    cfg = config or load_config()
    result = SchedulerResult()
    holder = "scheduler"

    ready = list_ready_tasks(session)
    slots_budget = max_to_start
    if slots_budget is None:
        # respect current effective capacity loosely; per-task evaluate decides
        slots_budget = cfg.settings.scheduler.max_concurrent_workers

    started = 0
    for task in ready:
        if started >= slots_budget:
            break

        result.attempted += 1
        if not acquire_lease(session, task, holder=holder):
            result.details.append({"task_id": task.id, "result": "lease_held"})
            continue

        verdict = cg.evaluate(session, task=task, config=cfg)
        if verdict.decision == CostDecision.DENY:
            result.denied += 1
            cg.deny_and_defer(session, task=task, verdict=verdict)
            release_lease(session, task)
            result.details.append(
                {"task_id": task.id, "result": "DENY", "reason": verdict.reason}
            )
            continue
        if verdict.decision in {CostDecision.DEFER, CostDecision.REQUIRE_APPROVAL}:
            result.deferred += 1
            cg.deny_and_defer(session, task=task, verdict=verdict)
            release_lease(session, task)
            result.details.append(
                {"task_id": task.id, "result": verdict.decision, "reason": verdict.reason}
            )
            continue
        # Include workers already started this cycle (mock finishes synchronously).
        active = cg.count_active_workers(session) + started
        if active >= verdict.effective_max_workers:
            result.throttled += 1
            release_lease(session, task)
            result.details.append(
                {
                    "task_id": task.id,
                    "result": "THROTTLE",
                    "reason": f"at_capacity:{active}/{verdict.effective_max_workers}",
                }
            )
            break

        # ALLOW (or THROTTLE with remaining slots)
        cg.reserve_budget(session, task=task)
        worker = hire_worker(session, task=task)
        record_audit(
            session,
            event_type="scheduler_dispatched",
            actor_type="system",
            actor_id="scheduler",
            target_type="task",
            target_id=task.id,
            metadata={
                "worker_id": worker.id,
                "decision": verdict.decision,
                "runner": runner,
            },
        )
        dispatch_worker(
            session,
            task=task,
            worker=worker,
            runner=runner,
            mock_succeed=mock_succeed,
            config=cfg,
            timeout_seconds=timeout_seconds,
        )
        release_lease(session, task)
        result.started += 1
        started += 1
        result.details.append(
            {
                "task_id": task.id,
                "result": "STARTED",
                "worker_id": worker.id,
                "runner": runner,
            }
        )

    return result
