"""Scheduler: lease READY tasks, consult Cost Governor, hire workers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from ai_company.config import AppConfig, load_config
from ai_company.db.models import Task
from ai_company.enums import CostDecision
from ai_company.services import cost_governor as cg
from ai_company.services.audit import record_audit
from ai_company.services.cleanup import run_cleanup
from ai_company.services.progress import emit as progress
from ai_company.services.staffing import (
    auto_approve_open_requests,
    note_staffing_hire,
)
from ai_company.services.tasks import list_ready_tasks
from ai_company.services.validation import is_judge_task, is_validation_task
from ai_company.services.workers import RunnerKind, dispatch_worker, hire_worker


@dataclass
class SchedulerResult:
    attempted: int = 0
    started: int = 0
    denied: int = 0
    deferred: int = 0
    throttled: int = 0
    leases_reclaimed: int = 0
    zombies_killed: int = 0
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
    cleanup -> READY -> Cost check -> reserve -> hire -> runner
    Re-fetches READY after each start so review tasks enqueued mid-cycle can run.
    """
    cfg = config or load_config()
    result = SchedulerResult()
    holder = "scheduler"

    cleanup = run_cleanup(session, config=cfg)
    result.leases_reclaimed = cleanup.leases_reclaimed
    result.zombies_killed = cleanup.zombies_killed
    auto_approve_open_requests(session, config=cfg)

    slots_budget = max_to_start
    if slots_budget is None:
        slots_budget = cfg.settings.scheduler.max_concurrent_workers

    started = 0
    started_validation = 0
    seen_blocked_lease: set[str] = set()
    started_ids: set[str] = set()
    started_validation_by_plan: dict[str, int] = {}
    started_by_role: dict[str, int] = {}

    while started < slots_budget:
        ready = list_ready_tasks(session)
        # Skip tasks already started or un-leaseable this cycle (avoid fail→READY→restart loops).
        task = next(
            (
                t
                for t in ready
                if t.id not in seen_blocked_lease and t.id not in started_ids
            ),
            None,
        )
        if task is None:
            break

        result.attempted += 1
        if not acquire_lease(session, task, holder=holder):
            seen_blocked_lease.add(task.id)
            result.details.append({"task_id": task.id, "result": "lease_held"})
            continue

        # Per-role capacity (Dynamic Staffing), including cycle-local starts
        from ai_company.services.staffing import (
            count_active_workers_for_role,
            role_capacity_for,
        )

        role_cap = role_capacity_for(task.role_required, cfg)
        if role_cap is not None:
            active_role = count_active_workers_for_role(
                session, role=task.role_required, project_id=task.project_id
            )
            cycle_role = started_by_role.get(task.role_required, 0)
            if active_role + cycle_role >= role_cap:
                result.throttled += 1
                release_lease(session, task)
                seen_blocked_lease.add(task.id)
                result.details.append(
                    {
                        "task_id": task.id,
                        "result": "THROTTLE",
                        "reason": (
                            f"role_capacity:{task.role_required}:"
                            f"{active_role + cycle_role}/{role_cap}"
                        ),
                    }
                )
                continue

        # Cycle-local validation fan-out (Cost Governor also enforces DB-active counts)        if is_validation_task(task) or is_judge_task(task):
            val_cap = cfg.settings.scheduler.max_validation_workers
            if (
                cg.count_active_validation_workers(session) + started_validation
                >= val_cap
            ):
                result.throttled += 1
                release_lease(session, task)
                seen_blocked_lease.add(task.id)
                result.details.append(
                    {
                        "task_id": task.id,
                        "result": "THROTTLE",
                        "reason": f"max_validation_workers_cycle:{val_cap}",
                    }
                )
                continue
            if is_validation_task(task) and task.validation_plan_id:
                parallel_cap = min(
                    cfg.budget.validation.max_parallel_reviewers_per_task,
                    cfg.validation.max_parallel_reviewers_per_task,
                )
                plan_n = started_validation_by_plan.get(task.validation_plan_id, 0)
                plan_n += cg.count_active_reviewers_for_plan(
                    session, task.validation_plan_id
                )
                if plan_n >= parallel_cap:
                    result.throttled += 1
                    release_lease(session, task)
                    seen_blocked_lease.add(task.id)
                    result.details.append(
                        {
                            "task_id": task.id,
                            "result": "THROTTLE",
                            "reason": f"max_parallel_reviewers:{parallel_cap}",
                        }
                    )
                    continue

        verdict = cg.evaluate(session, task=task, config=cfg)
        if verdict.decision == CostDecision.DENY:
            result.denied += 1
            cg.deny_and_defer(session, task=task, verdict=verdict)
            release_lease(session, task)
            seen_blocked_lease.add(task.id)
            result.details.append(
                {"task_id": task.id, "result": "DENY", "reason": verdict.reason}
            )
            continue
        if verdict.decision in {CostDecision.DEFER, CostDecision.REQUIRE_APPROVAL}:
            result.deferred += 1
            cg.deny_and_defer(session, task=task, verdict=verdict)
            release_lease(session, task)
            seen_blocked_lease.add(task.id)
            result.details.append(
                {"task_id": task.id, "result": verdict.decision, "reason": verdict.reason}
            )
            continue

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

        cg.reserve_budget(session, task=task)
        worker = hire_worker(session, task=task)
        note_staffing_hire(
            session, project_id=task.project_id, role=task.role_required
        )
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
        progress(
            f"START {task.kind}/{task.role_required}  {task.id}  "
            f"「{task.title}」  runner={runner}  worker={worker.id}"
        )
        execution = dispatch_worker(
            session,
            task=task,
            worker=worker,
            runner=runner,
            mock_succeed=mock_succeed,
            config=cfg,
            timeout_seconds=timeout_seconds,
        )
        session.refresh(task)
        outcome = "OK" if execution.success else "FAIL"
        extra = ""
        if execution.result_json:
            err = execution.result_json.get("parse_error")
            if err:
                extra = f"  parse_error={err}"
            v = execution.result_json.get("verdict")
            if v:
                extra += f"  verdict={v}"
        progress(
            f"DONE  {outcome}  {task.id}  status={task.status}  "
            f"exec={execution.id}{extra}"
        )
        release_lease(session, task)
        result.started += 1
        started += 1
        started_ids.add(task.id)
        started_by_role[task.role_required] = (
            started_by_role.get(task.role_required, 0) + 1
        )
        if is_validation_task(task) or is_judge_task(task):
            started_validation += 1
            if is_validation_task(task) and task.validation_plan_id:
                started_validation_by_plan[task.validation_plan_id] = (
                    started_validation_by_plan.get(task.validation_plan_id, 0) + 1
                )
        result.details.append(
            {
                "task_id": task.id,
                "result": "STARTED",
                "worker_id": worker.id,
                "runner": runner,
                "execution_id": execution.id,
                "success": str(bool(execution.success)),
            }
        )

    return result
